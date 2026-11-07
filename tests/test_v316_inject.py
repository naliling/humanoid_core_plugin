"""v3.1.6：问候时机卡 + 她上次主动开口的出处。

钉两件事。第一，问候窗口（早 7-11、午间、晚 21-24）要能翻成一张「可提的由头」进
上下文，但**只提供时机**：出处固定写「问候时机」，绝不暗示对方刚醒、刚吃完饭。
第二，她上一轮主动说过的话被追问时，缺的从来不是原话，是出处——有就带一条，找不到
只记码、一个字都不补。两件事都必须「core 侧函数不在也不崩」。

为什么这些边界值得钉：问候卡是唯一一张「现在能开口」的卡，一旦它被当成既成事实，
模型就会替对方宣布状态（「你刚醒吧」）；出处一旦靠猜，追问越真，编得越细。
"""

from __future__ import annotations

import importlib
import tempfile
import time
import unittest
from pathlib import Path

from humanoid.prompt_builder import STATE_MARK

from .fakes import FakeContext, FakeProvider
from .test_main_integration import (
    GOOD_SCHEDULE,
    SKIP_REASON,
    FakeEvent,
    FakeProviderRequest,
    injected_text,
    main_module,
)


def _card(
    title: str,
    *,
    kind: str = "greeting",
    detail: str = "",
    source: str = "问候时机",
    captured_at: float = 0.0,
    expires_at: float = 0.0,
    card_id: str = "",
) -> dict:
    """造一张与 CARD_KEYS 同形的 9 键卡（不多键：多一个键核心侧会判坏卡）。"""
    return {
        "id": card_id or f"{kind}:{title}",
        "kind": kind,
        "purpose": kind,
        "title": title,
        "detail": detail,
        "source": source,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "anchor_key": f"{kind}:{title}",
    }


class _Scope:
    def __init__(self, contract) -> None:
        self._contract = contract

    def get_self(self, key):
        return self._contract if key == "contract" else None


class _Core:
    def __init__(self, contract=None, signals=None) -> None:
        self.scope = _Scope(contract)
        self.signals = signals


class _Signals:
    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _plugin_diagnostics():
    """插件包内部的那份 diagnostics（见 test_v314_closing 的同名说明）。"""
    if main_module is None:  # pragma: no cover - 由 skipIf 兜住
        return None
    return importlib.import_module(main_module.__package__ + ".humanoid.diagnostics")


def _plugin_anchors():
    """插件包内部的 anchors：_greeting_cards 里 import 的就是这一份。"""
    if main_module is None:  # pragma: no cover
        return None
    return importlib.import_module(main_module.__package__ + ".humanoid.anchors")


@unittest.skipIf(main_module is None, SKIP_REASON)
class GreetingCardTests(unittest.TestCase):
    """纯函数：问候窗口 → 证据卡。"""

    NOW = 1_700_000_000.0

    def setUp(self) -> None:
        self.anchors = _plugin_anchors()
        self._had = hasattr(self.anchors, "greeting_opportunities")
        self._orig = getattr(self.anchors, "greeting_opportunities", None)

    def tearDown(self) -> None:
        if self._had:
            setattr(self.anchors, "greeting_opportunities", self._orig)
        else:
            try:
                delattr(self.anchors, "greeting_opportunities")
            except AttributeError:
                pass

    def _install(self, fn) -> None:
        setattr(self.anchors, "greeting_opportunities", fn)

    def test_greeting_card_gets_the_greeting_source(self):
        self._install(
            lambda contract, *, now=None: [
                _card("早安窗口", source="别的地方", expires_at=self.NOW + 3600)
            ]
        )
        cards = main_module._greeting_cards(_Core({"today": "x"}), now=self.NOW)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["source"], "问候时机", "出处必须写死，别让模型以为有别的来源")
        self.assertEqual(cards[0]["kind"], "greeting")
        self.assertEqual(cards[0]["title"], "早安窗口")

    def test_missing_function_returns_nothing_instead_of_crashing(self):
        # core 侧函数还没落盘 / 半升级：整块不注入，绝不能把这一轮回复拖垮。
        self._install(None)
        self.assertEqual(main_module._greeting_cards(_Core({"today": "x"})), [])

    def test_function_that_raises_returns_nothing(self):
        def boom(contract, *, now=None):
            raise RuntimeError("核心侧炸了")

        self._install(boom)
        self.assertEqual(main_module._greeting_cards(_Core({"today": "x"})), [])

    def test_missing_contract_returns_nothing(self):
        calls = []

        def fn(contract, *, now=None):
            calls.append(contract)
            return [_card("早安窗口")]

        self._install(fn)
        self.assertEqual(main_module._greeting_cards(_Core(None)), [])
        self.assertEqual(main_module._greeting_cards(_Core({})), [])
        self.assertEqual(calls, [], "契约取不到就不该去调核心侧函数")

    def test_old_signature_without_now_still_works(self):
        def fn(contract):
            return [_card("午间窗口")]

        self._install(fn)
        cards = main_module._greeting_cards(_Core({"today": "x"}), now=self.NOW)
        self.assertEqual([card["title"] for card in cards], ["午间窗口"])

    def test_bad_cards_are_skipped(self):
        self._install(lambda contract, *, now=None: ["坏的", None, _card("晚安窗口")])
        cards = main_module._greeting_cards(_Core({"today": "x"}))
        self.assertEqual([card["title"] for card in cards], ["晚安窗口"])

    def test_card_alive_follows_expiry(self):
        alive = main_module._card_alive
        self.assertTrue(alive(_card("早安窗口"), self.NOW), "没有有效期就当活着")
        self.assertTrue(alive(_card("早安窗口", expires_at=self.NOW + 1), self.NOW))
        self.assertFalse(alive(_card("早安窗口", expires_at=self.NOW), self.NOW))
        self.assertFalse(alive("坏的", self.NOW))


@unittest.skipIf(main_module is None, SKIP_REASON)
class MergeCardsTests(unittest.TestCase):
    """同一件事不该在上下文里出现两遍。"""

    def test_dedupes_by_id_and_keeps_first_order(self):
        first = _card("还书", kind="life", card_id="life:1")
        same = _card("还书", kind="life", card_id="life:1")
        other = _card("早安窗口")
        merged = main_module._merge_cards([first], [same, other])

        self.assertEqual([card["id"] for card in merged], ["life:1", "greeting:早安窗口"])

    def test_dedupes_without_id_by_anchor_and_source(self):
        one = _card("早安窗口", card_id="")
        two = _card("早安窗口", card_id="")
        merged = main_module._merge_cards([one], [two])
        self.assertEqual(len(merged), 1, "没有 id 时按 anchor_key+来源+标题去重")


@unittest.skipIf(main_module is None, SKIP_REASON)
class ProvenanceLineTests(unittest.TestCase):
    """她上次主动开口的出处：有则带，无则记码不补。"""

    NOW = 1_700_000_000.0

    def _line(self, payload, cards=None, now=None):
        core = _Core({"today": "x"}, _Signals(payload))
        return main_module._provenance_line(
            core, "99999", cards or [], self.NOW if now is None else now
        )

    def test_line_carries_source_and_capture_clock(self):
        payload = {
            "last_proactive_text": "刚路过一家花店",
            "last_proactive_at": self.NOW - 600,
            "provenance": {
                "card_id": "life:花店",
                "source": "日程",
                "captured_at": self.NOW - 1800,
            },
        }
        result = self._line(payload, [_card("路过花店", kind="life", card_id="life:花店")])

        self.assertFalse(result["missing"])
        self.assertIn("[日程]", result["line"], "出处要带出来，模型才知道这话从哪来")
        self.assertIn("路过花店", result["line"])
        self.assertIn("采集于", result["line"])
        self.assertIn("别补细节", result["line"], "只给由头，不给细节，细节靠她自己的记忆")

    def test_missing_provenance_flags_and_writes_nothing(self):
        payload = {
            "last_proactive_text": "刚路过一家花店",
            "last_proactive_at": self.NOW - 60,
            "provenance": {},
        }
        result = self._line(payload)

        self.assertTrue(result["missing"], "记录里没有出处就是没有，不许猜")
        self.assertEqual(result["line"], "", "找不到出处时上下文里一个字都不补")

    def test_flat_keys_still_work_for_old_records(self):
        payload = {
            "last_proactive_text": "刚路过一家花店",
            "last_proactive_at": self.NOW - 60,
            "last_proactive_source": "天气",
            "last_proactive_captured_at": self.NOW - 120,
        }
        result = self._line(payload)

        self.assertFalse(result["missing"])
        self.assertIn("[天气]", result["line"])

    def test_unknown_card_id_falls_back_to_source_only(self):
        payload = {
            "last_proactive_text": "刚路过一家花店",
            "last_proactive_at": self.NOW - 60,
            "provenance": {"card_id": "life:已删", "source": "日程"},
        }
        result = self._line(payload)

        self.assertFalse(result["missing"])
        self.assertIn("[日程]", result["line"])
        self.assertIn("只记到出处", result["line"], "卡没了就只说出处，别编标题")

    def test_record_without_text_is_silent(self):
        payload = {"last_proactive_at": self.NOW - 60, "provenance": {"source": "日程"}}
        result = self._line(payload)

        self.assertEqual(result, {"line": "", "missing": False}, "没主动说过就什么都不写")

    def test_stale_proactive_is_ignored(self):
        payload = {
            "last_proactive_text": "昨天问过早安",
            "last_proactive_at": self.NOW - 2 * 86400,
            "provenance": {"source": "日程"},
        }
        result = self._line(payload)

        self.assertEqual(result["line"], "", "一天前的由头只会把话题拽回去")

    def test_missing_signals_reader_is_silent(self):
        core = _Core({"today": "x"}, None)
        self.assertEqual(
            main_module._provenance_line(core, "99999", [], self.NOW),
            {"line": "", "missing": False},
        )

    def test_broken_signals_read_is_silent(self):
        core = _Core({"today": "x"}, _Signals(RuntimeError("读盘失败")))
        self.assertEqual(
            main_module._provenance_line(core, "99999", [], self.NOW),
            {"line": "", "missing": False},
        )


class _FakeFacade:
    """只实现注入路径用到的两个方法；名字与 CoreFacade 一致。"""

    def __init__(self, opportunities=None, materials=None) -> None:
        self._opp = list(opportunities or [])
        self._mat = list(materials or [])

    def opportunities(self, role_id):
        return list(self._opp)

    def materials(self, role_id):
        return list(self._mat)


@unittest.skipIf(main_module is None, SKIP_REASON)
class GreetingWiringTest(unittest.IsolatedAsyncioTestCase):
    """接线层：卡进没进请求、码记没记上、开关关掉是不是整块消失。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        self.ctx = FakeContext(chat_providers=[self.provider])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": ["10001"],
            "state_flush_interval_seconds": 1,
        }
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()
        self.diag = _plugin_diagnostics()
        self.diag.clear_reasons()
        self._orig_facade = main_module.get_core_facade
        self._orig_greeting = main_module._greeting_cards
        self._orig_provenance = main_module._provenance_line

    async def asyncTearDown(self) -> None:
        main_module.get_core_facade = self._orig_facade
        main_module._greeting_cards = self._orig_greeting
        main_module._provenance_line = self._orig_provenance
        await self.star.terminate()
        self.diag.clear_reasons()

    def _use_facade(self, facade) -> None:
        main_module.get_core_facade = lambda: facade

    def _codes(self) -> list[str]:
        # 问候/出处两码是上下文状态、默认不进跳过原因榜（见 CONTEXT_CODES）。
        return [
            item["code"]
            for item in self.diag.reason_report(include_context=True)["top"]
        ]

    async def test_greeting_card_joins_the_evidence_block(self) -> None:
        now = time.time()
        self._use_facade(_FakeFacade())
        # 注入侧调用时不传 now：默认值必须绑到真实时刻，否则 None + 3600 会被
        # fail-open 吞掉，测试表面全绿而卡其实一张都没进去。
        main_module._greeting_cards = lambda core, now=None, _now=now: [
            _card("早安窗口", expires_at=_now + 3600, captured_at=_now - 60)
        ]
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertIn("【她此刻可以提的具体的事】", text)
        self.assertIn("早安窗口", text, "问候窗口要变成一条能指认的由头")
        # v3.1.9：行里不再带出处标记（`[问候时机]` 那种括号已经被拿掉），
        # 但「窗口只提供时机、不宣布对方状态」这条语义不变。
        self.assertNotIn("[问候时机]", text)
        self.assertIn("（刚刚）", text, "一分钟前采集的窗口算新鲜")
        self.assertNotIn("你刚醒", text, "窗口只提供时机，不许替对方宣布状态")
        self.assertIn("greeting_opportunity", self._codes())

    async def test_expired_greeting_is_not_counted_as_used(self) -> None:
        now = time.time()
        self._use_facade(_FakeFacade())
        main_module._greeting_cards = lambda core, now=None, _now=now: [
            _card("早安窗口", expires_at=_now - 60)
        ]
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertNotIn("早安窗口", text, "窗口过了就不再提")
        self.assertNotIn("greeting_opportunity", self._codes())

    async def test_switch_off_skips_greeting_entirely(self) -> None:
        now = time.time()
        self.star._config_box.reload({**self.raw, "greeting_opportunity_enabled": False})
        self._use_facade(_FakeFacade())
        main_module._greeting_cards = lambda core, now=None, _now=now: [
            _card("早安窗口", expires_at=_now + 3600)
        ]
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertNotIn("早安窗口", text)
        self.assertNotIn("greeting_opportunity", self._codes())

    async def test_provenance_line_is_appended_after_the_cards(self) -> None:
        now = time.time()
        self._use_facade(_FakeFacade([_card("还书", kind="life", expires_at=now + 600)]))
        main_module._greeting_cards = lambda core, now=None: []
        main_module._provenance_line = lambda core, role_id, cards, now=None: {
            "line": "- 她上次主动开口的由头：[日程]还书；追问时就按这个说，别补细节。",
            "missing": False,
        }
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("你刚才说啥来着"), req)

        text = injected_text(req)
        self.assertIn("她上次主动开口的由头", text)
        self.assertIn("[日程]还书", text)
        self.assertLess(
            text.index("【她此刻可以提的具体的事】"),
            text.index("她上次主动开口的由头"),
            "出处接在证据卡后面，模型才知道它指的是哪一句",
        )

    async def test_missing_provenance_records_code_without_guessing(self) -> None:
        self._use_facade(_FakeFacade())
        main_module._greeting_cards = lambda core, now=None: []
        main_module._provenance_line = lambda core, role_id, cards, now=None: {
            "line": "",
            "missing": True,
        }
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("你刚才说啥来着"), req)

        text = injected_text(req)
        self.assertNotIn("她上次主动开口的由头", text, "没出处就一个字都不补")
        self.assertIn("share_provenance_missing", self._codes())

    async def test_greeting_failure_never_breaks_injection(self) -> None:
        # 用真的 _greeting_cards，并临时摘掉 core 侧函数：这条路径必须整块跳过而不是崩。
        # 为什么摘掉而不是依赖「函数还没落盘」：等 core 侧真的加了函数，晚上跑测试就会
        # 拿到一张晚安窗口卡，这条断言会随机翻脸。
        anchors = _plugin_anchors()
        had = hasattr(anchors, "greeting_opportunities")
        orig = getattr(anchors, "greeting_opportunities", None)
        try:
            if had:
                setattr(anchors, "greeting_opportunities", None)
            main_module._greeting_cards = self._orig_greeting
            self._use_facade(_FakeFacade())
            req = FakeProviderRequest("")
            await self.star.inject_context(FakeEvent("今天怎么样"), req)
        finally:
            if had:
                setattr(anchors, "greeting_opportunities", orig)

        text = injected_text(req)
        self.assertIn(STATE_MARK, text, "问候卡取不到也要照常注入「此刻的她」")
        self.assertNotIn("[问候时机]", text)

    async def test_evidence_lines_show_greeting_and_provenance(self) -> None:
        # 不进跳过原因榜，但报告里要有单独一行，排查「她怎么又空泛问候」时能一眼看到。
        self.diag.note_reason("greeting_opportunity", "1 条")
        self.diag.note_reason("share_provenance_missing", "group=False")

        line = " ".join(self.diag.evidence_lines())
        self.assertIn("问候时机", line)
        self.assertIn("出处缺失", line)
        self.assertFalse(
            [
                item["code"]
                for item in self.diag.reason_report()["top"]
                if item["code"] in ("greeting_opportunity", "share_provenance_missing")
            ],
            "上下文码不该出现在跳过原因榜里",
        )


if __name__ == "__main__":
    unittest.main()
