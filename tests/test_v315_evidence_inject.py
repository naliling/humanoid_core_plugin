"""v3.1.5：注入侧证据卡；v3.1.9 重做呈现层。

钉三件事：卡怎么进上下文、**过期的卡一定不进去**、一条都没有时要明说「没有，不要编」
而不是沉默。再加接线层：开关关掉整块不出现、真注入了才记 `opportunity_used`、
全过期记 `opportunity_expired`、一张没有记 `opportunity_missing`。

## v3.1.9 为什么把出处 / 采集时间 / 剩余有效期从行里拿掉

旧行是 `- [生活素材] 去楼下超市把缺的东西补上（她可能想做，不是已经做了）：超市，
大概 55 分钟（采集于09-26 13:00，还剩7 小时）`——一行 60 字，一半是免责声明和倒计时。
模型读到的是一张**待办清单**，于是用清单的口吻回话：「好的，那你可以去超市」。

现在每行只留 `- {title}：{detail}`，出处与时效交给「只有 2 小时内采集的才标（刚刚）」
这一条规则兜底：新鲜的才值得提，旧的干脆不提。引导语也从两句压成一句。

为什么这些边界值得钉：证据卡是「她说话有据可查」的唯一来源，它一旦退化成
「有什么说什么」，模型就会拿一条三小时前的由头聊现在——比没有由头更假。
"""

from __future__ import annotations

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
    kind: str = "life",
    detail: str = "",
    source: str = "日程",
    captured_at: float = 0.0,
    expires_at: float = 0.0,
) -> dict:
    """造一张形状与 `opportunity.make_card` 一致的卡（9 键全给）。"""
    return {
        "id": f"{kind}:{title}",
        "kind": kind,
        "purpose": "提一句",
        "title": title,
        "detail": detail,
        "source": source,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "anchor_key": title,
    }


@unittest.skipIf(main_module is None, SKIP_REASON)
class EvidenceNoteTests(unittest.TestCase):
    """纯函数：卡 → 一段可读的上下文。"""

    NOW = 1_700_000_000.0

    def _note(self, opportunities=None, materials=None, now=None):
        return main_module._evidence_note(
            opportunities or [], materials or [], self.NOW if now is None else now
        )

    def test_card_line_is_title_plus_detail_without_bookkeeping(self):
        card = _card(
            "下午去图书馆还书",
            detail="顺路买杯咖啡",
            captured_at=self.NOW - 600,
            expires_at=self.NOW + 7200,
        )
        result = self._note([card])

        self.assertEqual(result["used"], 1)
        self.assertEqual(result["expired"], 0)
        text = result["text"]
        self.assertIn("【她此刻可以提的具体的事】", text)
        self.assertIn("- 下午去图书馆还书：顺路买杯咖啡", text, "行格式是「标题：细节」")
        # 说明书腔的四个来源全部清掉。
        self.assertNotIn("[日程]", text, "出处不再进正文")
        self.assertNotIn("采集于", text)
        self.assertNotIn("还剩", text)
        self.assertNotIn("她可能想做", text)
        self.assertNotIn("分钟", text)
        # 采集于 10 分钟前：够新鲜，行尾标「（刚刚）」；有效期不写。
        self.assertIn("（刚刚）", text)
        self.assertEqual(result["shown"], [card])

    def test_old_card_gets_no_freshness_mark(self):
        card = _card("上周去了趟花市", captured_at=self.NOW - 3 * 3600)
        text = self._note([card])["text"]
        self.assertIn("上周去了趟花市", text)
        self.assertNotIn("（刚刚）", text, "超过 2 小时的就不该再假装刚发生")

    def test_lead_is_one_sentence(self):
        card = _card("还书", captured_at=self.NOW - 60)
        text = self._note([card])["text"]
        lead = [line for line in text.splitlines() if line.startswith("这些是")]
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead[0], "这些是今天真有的、可能做的事，挑一件说，别编。")

    def test_materials_are_listed_after_opportunities(self):
        opp = _card("还书", expires_at=self.NOW + 3600)
        mat = _card("想下楼走走", kind="activity", source="生活素材", expires_at=self.NOW + 3600)
        text = self._note([opp], [mat])["text"]

        self.assertLess(text.index("还书"), text.index("想下楼走走"))

    def test_empty_cards_say_there_is_nothing_and_do_not_make_things_up(self):
        result = self._note([], [])

        self.assertEqual(result["used"], 0)
        self.assertEqual(result["expired"], 0)
        self.assertIn("【暂时没有特别的事】", result["text"])
        self.assertIn("别硬找由头", result["text"], "没有由头时必须明说，沉默挡不住编造")
        # v3.1.9：不再教她「宁可只回一句」——那是让她少说话，不是让她说得像人。
        self.assertNotIn("宁可只回一句", result["text"])

    def test_expired_card_is_dropped(self):
        card = _card("昨天去看电影", expires_at=self.NOW - 1)
        result = self._note([card])

        self.assertEqual(result["used"], 0)
        self.assertEqual(result["expired"], 1)
        self.assertNotIn("昨天去看电影", result["text"], "过时的由头比没有更假")
        self.assertIn("【暂时没有特别的事】", result["text"])

    def test_expired_is_counted_even_when_others_are_usable(self):
        good = _card("还书", expires_at=self.NOW + 600)
        stale = _card("昨天去看电影", expires_at=self.NOW - 600)
        result = self._note([good, stale])

        self.assertEqual(result["used"], 1)
        self.assertEqual(result["expired"], 1)
        self.assertIn("还书", result["text"])
        self.assertNotIn("昨天去看电影", result["text"])

    def test_card_without_expiry_omits_the_ttl(self):
        card = _card("她今天在整理旧照片", captured_at=self.NOW)
        text = self._note([card])["text"]

        self.assertIn("整理旧照片", text)
        self.assertNotIn("还剩", text, "没有有效期就不要编一个出来")

    def test_broken_cards_are_ignored_instead_of_crashing(self):
        result = self._note([None, {}, {"title": "   "}, "不是字典", {"title": None}])

        self.assertEqual(result["used"], 0)
        self.assertIn("【暂时没有特别的事】", result["text"])

    def test_list_is_capped(self):
        cards = [
            _card(f"第{index}件事", expires_at=self.NOW + 3600) for index in range(15)
        ]
        result = self._note(cards)

        # v3.1.9：上限从 10 降到 6——8 条 × 60 字里一半是模板，占的是 token，
        # 换来的是模型把清单当任务列表。
        self.assertEqual(main_module._EVIDENCE_LIMIT, 6)
        self.assertEqual(result["used"], main_module._EVIDENCE_LIMIT)
        self.assertEqual(
            sum(1 for line in result["text"].splitlines() if line.startswith("- ")),
            main_module._EVIDENCE_LIMIT,
        )

    def test_span_text_buckets(self):
        span = main_module._span_text
        self.assertEqual(span(30), "30 秒")
        self.assertEqual(span(600), "10 分钟")
        self.assertEqual(span(7200), "2 小时")
        self.assertEqual(span(2 * 86400), "2 天")
        self.assertEqual(span("坏了"), "")


def _plugin_diagnostics():
    """插件包内部的那份 diagnostics（见 test_v314_closing 的同名说明）。"""
    import importlib

    if main_module is None:  # pragma: no cover - 由 skipIf 兜住
        return None
    return importlib.import_module(main_module.__package__ + ".humanoid.diagnostics")


class _FakeFacade:
    """只实现注入路径用到的两个方法；名字与 CoreFacade 一致。"""

    def __init__(self, opportunities=None, materials=None) -> None:
        self._opp = list(opportunities or [])
        self._mat = list(materials or [])
        self.calls = 0

    def opportunities(self, role_id):
        self.calls += 1
        return list(self._opp)

    def materials(self, role_id):
        return list(self._mat)


@unittest.skipIf(main_module is None, SKIP_REASON)
class EvidenceWiringTest(unittest.IsolatedAsyncioTestCase):
    """接线层：块有没有真的进请求、原因码有没有真的记上、开关有没有真的生效。"""

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

    async def asyncTearDown(self) -> None:
        main_module.get_core_facade = self._orig_facade
        await self.star.terminate()
        self.diag.clear_reasons()

    def _use_facade(self, facade) -> None:
        main_module.get_core_facade = lambda: facade

    def _codes(self) -> list[str]:
        # 证据卡三码属于上下文状态、默认不进跳过原因榜（见 CONTEXT_CODES），
        # 这里要看全量，所以带上 include_context。
        return [
            item["code"]
            for item in self.diag.reason_report(include_context=True)["top"]
        ]

    async def test_cards_reach_the_request_without_bookkeeping(self) -> None:
        now = time.time()
        self._use_facade(
            _FakeFacade(
                opportunities=[
                    _card("下午去图书馆还书", captured_at=now - 60, expires_at=now + 7200)
                ]
            )
        )
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertIn("【她此刻可以提的具体的事】", text)
        self.assertIn("下午去图书馆还书", text)
        self.assertIn("（刚刚）", text, "一分钟前采集的，行尾标新鲜")
        self.assertNotIn("[日程]", text, "出处不再进正文")
        self.assertNotIn("还剩", text, "倒计时不再进正文")
        self.assertIn("opportunity_used", self._codes())

    async def test_no_cards_tells_her_to_say_less(self) -> None:
        self._use_facade(_FakeFacade())
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertIn("【暂时没有特别的事】", text)
        self.assertIn("别硬找由头", text)
        self.assertIn("opportunity_missing", self._codes())

    async def test_expired_cards_are_not_injected(self) -> None:
        now = time.time()
        self._use_facade(
            _FakeFacade(
                opportunities=[_card("昨天去看电影", expires_at=now - 60)],
                materials=[_card("想下楼走走", kind="activity", expires_at=now - 1)],
            )
        )
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertNotIn("昨天去看电影", text)
        self.assertNotIn("想下楼走走", text)
        self.assertIn("【暂时没有特别的事】", text)
        self.assertIn("opportunity_expired", self._codes())

    async def test_switch_off_removes_the_block_entirely(self) -> None:
        self.star._config_box.reload({**self.raw, "opportunity_evidence_enabled": False})
        self._use_facade(_FakeFacade([_card("还书", expires_at=time.time() + 600)]))
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertNotIn("【她此刻可以提的具体的事】", text)
        self.assertNotIn("【暂时没有特别的事】", text)
        self.assertTrue(text.strip(), "关掉证据卡不该连其他注入一起丢")
        self.assertFalse(
            [code for code in self._codes() if code.startswith("opportunity_")],
            "开关关掉时不该再记任何证据卡原因码",
        )

    async def test_opportunity_codes_do_not_crowd_out_skip_reasons(self) -> None:
        # 证据卡三码每轮都记一次：若参与跳过原因排名，它们会凭条数长期霸榜，把真正
        # 让她不回话的原因（CCB 冷却之类）挤下去。这里钉住「默认榜里没有它们」。
        self._use_facade(_FakeFacade())
        for _ in range(3):
            await self.star.inject_context(FakeEvent("今天怎么样"), FakeProviderRequest(""))
        self.diag.note_reason("ccb_cooldown", "冷却中")

        top_codes = [item["code"] for item in self.diag.reason_report()["top"]]
        self.assertIn("ccb_cooldown", top_codes)
        self.assertFalse(
            [code for code in top_codes if code.startswith("opportunity_")],
            "证据卡码不该出现在跳过原因榜里",
        )
        self.assertIn("opportunity_missing", self._codes(), "带上 include_context 时仍要能看到")

    async def test_evidence_lines_summarize_context_codes(self) -> None:
        # 不抢榜不等于看不见：报告里单独一行【证据卡】，一眼能看出她最近是不是一直没素材。
        self._use_facade(_FakeFacade())
        await self.star.inject_context(FakeEvent("今天怎么样"), FakeProviderRequest(""))
        self.diag.note_reason("opportunity_expired", "全过期")

        lines = self.diag.evidence_lines()
        self.assertTrue(lines, "有证据卡记录时该出现【证据卡】小节")
        self.assertIn("【证据卡】", lines[0])
        self.assertIn("一张都没有 ×1", lines[0])
        self.assertIn("全过期 ×1", lines[0])

        self.diag.clear_reasons()
        self.assertEqual(self.diag.evidence_lines(), [], "没有记录就不出现，不给报告添空行")

    async def test_facade_failure_never_breaks_injection(self) -> None:
        class _BrokenFacade:
            def opportunities(self, role_id):
                raise RuntimeError("门面坏了")

            def materials(self, role_id):
                raise RuntimeError("门面坏了")

        self._use_facade(_BrokenFacade())
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("今天怎么样"), req)

        text = injected_text(req)
        self.assertIn(STATE_MARK, text, "证据卡坏了也要照常注入「此刻的她」")
        self.assertNotIn("【她此刻可以提的具体的事】", text)


if __name__ == "__main__":
    unittest.main()
