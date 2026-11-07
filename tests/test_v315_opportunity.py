"""v3.1.5：Core 侧证据卡（由头 / 素材）。

社交层主动开口缺的从来不是「她在休息」这种状态，而是「她今天确实有什么」。这一层
把 Core 手里散着的事实翻成统一的卡，交给社交层当由头。要守住的六件事：

1. **卡是稳定的**：同一件事跨轮、跨进程同一个 `id` 与 `anchor_key`——社交层拿它们
   记「这条用过了」，靠文本比会漏（换一种说法就认不出是同一件事）。
2. **同一小时内逐字节相同**：契约是「变了才重写」，卡每次抖一下就等于每分钟重写
   state.json。种子按「角色 + 日期」、采集时间按小时取整，就是为了这个。
3. **过期的不发**：天气 3 小时、刚发生的事 2 小时、节日 24 小时。过期的由头比没有
   更假（「今天下雨」说在晴天里）。
4. **两个键永远都在**：没有卡就是空列表。社交层按「键在不在」判新旧版本，缺键与
   空列表是两件事。
5. **绝不让契约生成失败**：卡是锦上添花，任何一处翻车都只让这两个键变空列表。
6. **只加不改**：schema 仍是 humanoid.contract/3，旧键一个不动。
"""

from __future__ import annotations

import json
import unittest

from humanoid import anchors, opportunity
from humanoid.api import CoreFacade
from humanoid.link import CONTRACT_SCHEMA, build_contract
from humanoid.services import life_material

from .test_link import FROZEN, Harness, freeze

HOUR = 3600.0
NOW = 1_790_000_000.0  # 2026-09-22 前后的某个整点附近，用固定值让断言可复现


def card(kind="life", title="在阳台晒被子", at=NOW, **kwargs):
    return opportunity.make_card(
        kind=kind, title=title, captured_at=at, **kwargs
    )


class CardTest(unittest.TestCase):
    """卡本身：形状、稳定 id、有效期、过期与去重。"""

    def test_card_has_exactly_the_nine_keys(self):
        self.assertEqual(
            set(card()),
            {
                "id", "kind", "purpose", "title", "detail", "source",
                "captured_at", "expires_at", "anchor_key",
            },
        )

    def test_same_thing_same_id_across_builds(self):
        """同一件事两次造卡必须同一个 id：社交层靠它记「这条用过了」。"""
        self.assertEqual(card()["id"], card()["id"])

    def test_id_ignores_punctuation_and_spacing(self):
        a = opportunity.make_card(kind="life", title="在阳台晒被子。", captured_at=NOW)
        b = opportunity.make_card(kind="life", title="在阳台晒被子", captured_at=NOW)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["anchor_key"], b["anchor_key"])

    def test_different_kinds_never_collide(self):
        a = opportunity.make_card(kind="life", title="看猫", captured_at=NOW)
        b = opportunity.make_card(kind="journal", title="看猫", captured_at=NOW)
        self.assertNotEqual(a["id"], b["id"])

    def test_ttl_is_per_kind(self):
        """有效期从**采集时间**（已按小时取整）起算，不是从「现在」起算。"""
        item = card(kind="weather")
        self.assertEqual(
            item["expires_at"] - item["captured_at"],
            opportunity.OPP_TTL_SECONDS["weather"],
        )
        self.assertLess(
            opportunity.OPP_TTL_SECONDS["activity"],
            opportunity.OPP_TTL_SECONDS["holiday"],
        )

    def test_hour_bucket_floors_to_the_hour(self):
        aligned = opportunity.hour_bucket(NOW)
        self.assertEqual(aligned % HOUR, 0.0)
        self.assertEqual(opportunity.hour_bucket(aligned + 59 * 60), aligned)
        self.assertNotEqual(opportunity.hour_bucket(aligned + 61 * 60), aligned)

    def test_unknown_kind_and_empty_title_are_rejected(self):
        with self.assertRaises(ValueError):
            opportunity.make_card(kind="没这个种类", title="x", captured_at=NOW)
        with self.assertRaises(ValueError):
            opportunity.make_card(kind="life", title="   ", captured_at=NOW)

    def test_expired_cards_are_dropped(self):
        fresh = card(kind="weather", at=NOW)
        stale = card(kind="weather", title="昨天的大雨", at=NOW - 6 * HOUR)
        kept = opportunity.fresh_cards([stale, fresh], NOW + 1)
        self.assertEqual([item["title"] for item in kept], ["在阳台晒被子"])

    def test_dedupe_keeps_the_newest_of_one_anchor(self):
        old = card(title="在阳台晒被子", at=NOW)
        new = card(title="在阳台晒被子", at=NOW + 2 * HOUR)
        kept = opportunity.dedupe([old, new])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["captured_at"], opportunity.hour_bucket(NOW + 2 * HOUR))

    def test_fresh_cards_respects_the_limit(self):
        cards = [card(title="第 " + str(i) + " 件事", at=NOW + i) for i in range(9)]
        self.assertEqual(len(opportunity.fresh_cards(cards, NOW + 100)), opportunity.OPP_LIMIT)

    def test_contract_blocks_always_returns_both_keys(self):
        blocks = opportunity.contract_blocks([], [], NOW)
        self.assertEqual(set(blocks), {"opportunities", "materials"})
        self.assertEqual(blocks["opportunities"], [])
        self.assertEqual(blocks["materials"], [])


class AnchorsTest(unittest.TestCase):
    """由头 → 卡：契约里那几种形状各自翻得对，坏形状不炸。"""

    CONTRACT = {
        "day": {
            "doing": "在厨房煮面",
            "done": ["下午在阳台晒被子", "傍晚把碗推到水池泡着"],
            "wants": [],
            "dreams": ["梦见一只很大的猫"],
        },
        "wants": ["想把照片整理成册"],
        "ongoing": ["在给旧相机换皮套"],
        "event": {"phrase": "被夸了一句"},
        "weather": "小雨",
        "holiday": "中秋",
    }

    def setUp(self):
        self.cards = anchors.cards_from_contract(self.CONTRACT, now=NOW)

    def _titles(self):
        return [item["title"] for item in self.cards]

    def test_every_fact_becomes_a_card(self):
        titles = self._titles()
        for expected in (
            "在厨房煮面", "下午在阳台晒被子", "想把照片整理成册",
            "在给旧相机换皮套", "被夸了一句", "梦见一只很大的猫", "小雨", "中秋",
        ):
            self.assertIn(expected, titles)

    def test_sources_are_readable_chinese(self):
        sources = {item["source"] for item in self.cards}
        self.assertIn(anchors.SOURCE_DAY, sources)
        self.assertIn(anchors.SOURCE_WANT, sources)
        self.assertIn(anchors.SOURCE_ONGOING, sources)
        self.assertIn(anchors.SOURCE_EVENT, sources)
        self.assertIn(anchors.SOURCE_DREAM, sources)
        self.assertIn(anchors.SOURCE_WEATHER, sources)
        self.assertIn(anchors.SOURCE_HOLIDAY, sources)

    def test_kinds_are_fixed_not_guessed(self):
        by_title = {item["title"]: item["kind"] for item in self.cards}
        self.assertEqual(by_title["在厨房煮面"], "activity")
        self.assertEqual(by_title["在给旧相机换皮套"], "reminder")
        self.assertEqual(by_title["梦见一只很大的猫"], "journal")
        self.assertEqual(by_title["小雨"], "weather")
        self.assertEqual(by_title["中秋"], "holiday")

    def test_junk_contract_does_not_raise(self):
        for junk in (None, [], "字符串", {}, {"day": "不是字典"}, {"day": {"done": 3}}):
            self.assertEqual(anchors.cards_from_contract(junk, now=NOW), [])

    def test_duplicate_texts_collapse(self):
        contract = {"day": {"done": ["看猫", "看猫", " 看猫 "]}}
        cards = anchors.cards_from_contract(contract, now=NOW)
        self.assertEqual(len(cards), 1)

    def test_same_hour_builds_are_byte_identical(self):
        """同一小时内两次 build 的这两个键必须逐字节相同，否则每分钟重写 state.json。"""
        core = _FakeCore()
        first = anchors.contract_blocks(core, self.CONTRACT, now=NOW)
        second = anchors.contract_blocks(core, self.CONTRACT, now=NOW + 60)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_materials_are_capped_and_ttl_is_eight_hours(self):
        blocks = anchors.contract_blocks(_FakeCore(), self.CONTRACT, now=NOW)
        self.assertLessEqual(len(blocks["materials"]), opportunity.MATERIAL_LIMIT)
        self.assertTrue(blocks["materials"])
        for item in blocks["materials"]:
            self.assertEqual(
                item["expires_at"] - item["captured_at"], opportunity.MATERIAL_TTL_SECONDS
            )

    def test_broken_core_still_yields_the_two_keys(self):
        blocks = anchors.contract_blocks(_FakeCore(broken=True), self.CONTRACT, now=NOW)
        self.assertEqual(set(blocks), {"opportunities", "materials"})
        self.assertTrue(blocks["opportunities"])


class MaterialCardTest(unittest.TestCase):
    """生活素材 → 卡：出处念得出来，题眼用于去重。"""

    ITEM = {
        "event": "去楼下超市把缺的东西补上",
        "location": "超市",
        "minutes": (40, 70),
        "topic": "买菜",
        "band": "life",
        "slots": ("下午",),
        "needs": (),
        "interest": "",
        "wants": "",
    }

    def test_source_is_the_band_in_chinese(self):
        self.assertEqual(life_material.material_source(self.ITEM), "生活素材")
        duty = dict(self.ITEM, band="duty")
        self.assertEqual(life_material.material_source(duty), "责任")
        unknown = dict(self.ITEM, band="没这个档")
        self.assertEqual(life_material.material_source(unknown), "生活素材")

    def test_card_uses_topic_as_anchor_and_location_in_detail(self):
        item = life_material.material_card(self.ITEM, captured_at=NOW, seed=["bot1", "2026-08-22"])
        self.assertEqual(item["title"], self.ITEM["event"])
        self.assertEqual(item["anchor_key"], opportunity.anchor_key_for("life", "买菜"))
        self.assertIn("超市", item["detail"])

    def test_bad_items_are_dropped_one_by_one(self):
        good = life_material.material_card(self.ITEM, captured_at=NOW)
        cards = life_material.materials_as_cards(
            [self.ITEM, {"event": ""}, "不是字典"], captured_at=NOW
        )
        self.assertEqual([item["id"] for item in cards], [good["id"]])

    def test_pool_cards_are_deterministic_within_the_hour(self):
        core = _FakeCore()
        first = anchors.material_cards(core, {}, now=NOW)
        second = anchors.material_cards(core, {}, now=NOW + 59 * 60)
        self.assertEqual([item["id"] for item in first], [item["id"] for item in second])


class ContractMountTest(unittest.TestCase):
    """契约落盘：两个新键在、旧的不动、旧 state 也会被重写一次。"""

    def setUp(self):
        self.harness = Harness()
        self.core = freeze(self.harness.roles.get_or_create("bot1"))

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_contract_carries_both_card_blocks(self):
        contract = build_contract(self.core)
        self.assertIn("opportunities", contract)
        self.assertIn("materials", contract)
        self.assertIsInstance(contract["opportunities"], list)
        self.assertIsInstance(contract["materials"], list)
        for item in contract["opportunities"] + contract["materials"]:
            self.assertEqual(opportunity.validate_card(item), [])

    def test_schema_and_capabilities_are_untouched(self):
        """只加键：schema 仍是 3，旧能力一个不少。"""
        contract = build_contract(self.core)
        self.assertEqual(contract["schema"], CONTRACT_SCHEMA)
        for name in ("body", "feelings", "form", "activity", "day", "event", "identity", "outfit"):
            self.assertIn(name, contract["capabilities"])

    def test_same_hour_contract_is_stable(self):
        first = build_contract(self.core)
        second = build_contract(self.core)
        for key in ("opportunities", "materials"):
            self.assertEqual(
                json.dumps(first[key], ensure_ascii=False, sort_keys=True),
                json.dumps(second[key], ensure_ascii=False, sort_keys=True),
                key,
            )

    def test_old_state_without_cards_gets_rewritten(self):
        """旧 state 里没有这两个键时，其余键都没变也要重写一次，否则永远读不到卡。"""
        stored = self.core.refresh_contract()
        self.assertIsNotNone(stored)
        stripped = {
            key: value for key, value in stored.items()
            if key not in ("opportunities", "materials")
        }
        self.core.scope.set_self("contract", stripped)
        # 摘要缓存会让它直接返回盘上那份；这里模拟「新版本第一次跑」。
        self.core._contract_digest = None
        rebuilt = self.core.refresh_contract()
        self.assertIsNotNone(rebuilt)
        self.assertIn("opportunities", rebuilt)
        self.assertIn("materials", rebuilt)


class FacadeCardsTest(unittest.TestCase):
    """门面：只读、不抛、旧契约也能现算。"""

    def setUp(self):
        self.harness = Harness()
        self.core = freeze(self.harness.roles.get_or_create("bot1"))
        self.facade = CoreFacade(self.harness.roles)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_unknown_role_returns_empty_list(self):
        self.assertEqual(self.facade.opportunities("不存在的角色"), [])
        self.assertEqual(self.facade.materials("不存在的角色"), [])

    def test_stored_cards_are_returned_as_is(self):
        stored = [card(kind="weather", title="外面的小雨")]
        self.core.scope.set_self("contract", {"opportunities": stored, "materials": []})
        self.assertEqual(self.facade.opportunities("bot1"), stored)

    def test_old_contract_is_computed_on_demand(self):
        """升级当天盘上还是旧契约（没有这两个键）：现算一次，不能全空。"""
        self.core.scope.set_self(
            "contract", {"day": {"done": ["下午在阳台晒被子"]}, "weather": "小雨"}
        )
        cards = self.facade.opportunities("bot1")
        self.assertIsInstance(cards, list)
        self.assertTrue(cards, "旧契约也要能现算出由头")

    def test_facade_never_raises_on_a_broken_contract(self):
        self.core.scope.set_self("contract", {"day": "不是字典", "wants": 3})
        self.assertIsInstance(self.facade.opportunities("bot1"), list)
        self.assertIsInstance(self.facade.materials("bot1"), list)


class _FakeCore:
    """只够 anchors 用：角色 id、人设、scope。"""

    role_id = "bot1"

    def __init__(self, broken: bool = False) -> None:
        self._broken = broken

    def _persona_text(self):
        if self._broken:
            raise RuntimeError("人设读不到")
        return "一个安静的姑娘，养了一只橘猫，喜欢看书。"

    @property
    def scope(self):
        if self._broken:
            raise RuntimeError("scope 坏了")

        class _Scope:
            def get_self(self, key, default=None):
                return default

        return _Scope()
