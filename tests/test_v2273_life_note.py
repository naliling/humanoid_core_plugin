"""「她手上还开着的事」——跨轮、会自己结束的未完事项（life_log + 事实块注入）。

要守住的五件事，错了任何一件，这一层要么白加、要么比不加更坏：

1. **整体替换。** 社交层不再发布某一条，那条就算结了。只增不减会退化成一份永远
   清不掉的账，表现就是「她一直在准备某件事」——过程系统以前锁死过的那副样子。
2. **读不到 ≠ 全结了。** 信号文件缺失/过期时读回来也是空，拿它清账会在社交层停摆
   期间无声抹掉她记着的事。清账只该发生在文件**新鲜**的时候。
3. **它走事实块，不另起标题。** `prompt_builder._sentence` 的规矩是「没有标题、没有
   标签——标签是束缚词」。给它一个【标题】+ 正文的独立段落，最容易被模型当成「要念
   出来的内容」，一开口就是报备。
4. **渲染必须确定。** 同一批内容每轮渲染出不同文本，事实块每轮重发、每轮都不一样
   就等于白占预算。
5. **渲染必须变化很慢。** 所以只用「刚刚/几小时前/昨天/前天/N 天前」这类天级说法，
   不含当前钟点、不含今天的日期。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid import life_log
from humanoid.config import ConfigBox
from humanoid.engine import HumanoidEngine
from humanoid.link import SIGNALS_FILE_NAME, SOCIAL_PLUGIN_DIR_NAME
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.prompt_builder import STATE_MARK
from humanoid.role_manager import RoleManager
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger
from .test_main_integration import FakeProviderRequest, main_module

TZ = ZoneInfo("Asia/Shanghai")
FROZEN = datetime(2026, 8, 22, 15, 0, tzinfo=TZ)
NOW = FROZEN.timestamp()

# 指令词：这一层是**事实**，不是对模型的要求（`test_v2151_freedom` 盯的是同一个口径）。
FORBIDDEN = ("不要", "必须", "别", "务必", "别忘了", "记得")


def item(text: str, uid: str = "10001", at: float = NOW) -> dict:
    return {"text": text, "uid": uid, "at": at}


class LifeLogTest(unittest.TestCase):
    """纯逻辑：解析、合并、过期、措辞。不碰 IO。"""

    def test_replacement_is_how_an_item_ends(self):
        first = life_log.merge_open([], [item("甲"), item("乙")], NOW)
        self.assertEqual([e["text"] for e in first], ["甲", "乙"])
        second = life_log.merge_open(first, [item("乙")], NOW)
        self.assertEqual([e["text"] for e in second], ["乙"], "不再发布的那条应当算结了")

    def test_junk_is_tolerated(self):
        merged = life_log.merge_open(
            [],
            [None, "字符串", {}, {"text": "   "}, {"text": "有效", "uid": "u", "at": "x", "没见过的键": 1}],
            NOW,
        )
        self.assertEqual([e["text"] for e in merged], ["有效"], "认不出的元素与字段一律丢掉")

    def test_not_a_list_is_empty(self):
        for junk in (None, {}, "文本", 3, {"open_items": "x"}):
            self.assertEqual(life_log.parse_open(junk), [])

    def test_capped_and_truncated(self):
        many = [item(f"第{i}件") for i in range(10)]
        self.assertEqual(len(life_log.merge_open([], many, NOW)), life_log.MAX_OPEN)
        merged = life_log.merge_open([], [item("很长的正文" * 20)], NOW)
        self.assertLessEqual(len(merged[0]["text"]), life_log.TEXT_MAX_CHARS)

    def test_duplicate_texts_take_one_slot(self):
        """社交层那边 loop 与 promise 可能同时命中同一件事，别让它占两个名额。"""
        merged = life_log.merge_open([], [item("同一件事"), item("同一件事", uid="other")], NOW)
        self.assertEqual(len(merged), 1)

    def test_expiry_uses_her_own_clock(self):
        merged = life_log.merge_open([], [item("甲")], NOW)
        self.assertTrue(merged[0]["until"] > 0, "到期时刻必须由 Core 按自己的钟算")
        self.assertEqual(len(life_log.prune(merged, NOW + 3600)), 1)
        self.assertEqual(life_log.prune(merged, NOW + 3.5 * 86400), [], "三天半后该到期了")

    def test_unknown_deadline_is_not_expiry(self):
        """`until` 是 0 表示「不知道什么时候到期」，不是「已经过期」。

        拿 0 当过期，会在状态文件被写坏一次之后静默清空整本账。
        """
        unknown = [{"text": "甲", "uid": "", "at": 0.0, "until": 0.0, "src": "social"}]
        self.assertEqual(len(life_log.prune(unknown, NOW)), 1)

    def test_untrusted_now_changes_nothing(self):
        merged = life_log.merge_open([], [item("甲")], NOW)
        self.assertEqual(len(life_log.prune(merged, 0)), 1)
        self.assertEqual(len(life_log.prune(merged, float("nan"))), 1)

    def test_order_is_deterministic(self):
        """同一批内容渲染两次必须是同一个字符串。"""
        same_at = [item("乙"), item("甲"), item("丙")]
        lines = life_log.render_lines(same_at, NOW)
        # 同一个人的几件事合成一句，同一时刻按正文排序。
        # 汉字按码位比：丙(U+4E19) < 乙(U+4E59) < 甲(U+7532)。
        self.assertEqual(len(lines), 1, lines)
        body = lines[0].split("：", 1)[1]
        self.assertEqual([piece[:1] for piece in body.split("、")], ["丙", "乙", "甲"])
        self.assertEqual(lines, life_log.render_lines(list(reversed(same_at)), NOW))

    def test_same_person_merges_into_one_sentence(self):
        """连着写两遍「还有件事没完」就是一份清单，而这是她心里的一句话。"""
        lines = life_log.render_lines([item("甲"), item("乙")], NOW)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0].count("还有"), 1, "抬头只该出现一次")

    def test_different_people_stay_separate(self):
        lines = life_log.render_lines([item("甲", uid="1"), item("乙", uid="2")], NOW)
        self.assertEqual(len(lines), 2, "跟谁的事不能混成一句")

    def test_newest_first(self):
        lines = life_log.render_lines([item("旧的", at=NOW - 3 * 86400), item("新的", at=NOW - 60)], NOW)
        self.assertLess(lines[0].index("新的"), lines[0].index("旧的"))

    def test_untrusted_at_sorts_last(self):
        """说不清什么时候的事最不该占位置：`max_open` 截断时它先掉。"""
        lines = life_log.render_lines([item("没有时间", at=0.0), item("有时间的")], NOW, max_open=1)
        self.assertIn("有时间的", lines[0])
        self.assertNotIn("没有时间", lines[0])

    def test_ago_text_is_day_granular(self):
        self.assertEqual(life_log.ago_text(NOW - 60, NOW), "刚刚")
        self.assertEqual(life_log.ago_text(NOW - 5 * 3600, NOW), "几小时前")
        self.assertEqual(life_log.ago_text(NOW - 20 * 3600, NOW), "昨天")
        self.assertEqual(life_log.ago_text(NOW - 40 * 3600, NOW), "前天")
        self.assertEqual(life_log.ago_text(NOW - 5 * 86400, NOW), "5 天前")

    def test_ago_text_gives_up_instead_of_guessing(self):
        self.assertEqual(life_log.ago_text(0, NOW), "")
        self.assertEqual(life_log.ago_text(NOW, 0), "")
        self.assertEqual(life_log.ago_text(NOW + 100, NOW), "", "钟被回拨过就不说")

    def test_rendered_text_is_facts_not_orders(self):
        merged = life_log.merge_open([], [item("他上次说的那个面试"), item("要给他回的那封信")], NOW)
        text = "\n".join(life_log.render_lines(merged, NOW))
        for word in FORBIDDEN:
            self.assertNotIn(word, text, f"渲染里出现了指令词「{word}」")

    def test_no_trailing_period(self):
        """片段不带句号：事实块由 `_sentence` 用「，」拼句、`_finish` 末尾统一补「。」。

        自带句号会拼出「…，…。，…。」这种断句，正是「一段一段的文本」的味道。
        """
        for line in life_log.render_lines([item("甲"), item("乙")], NOW):
            self.assertFalse(line.endswith("。"), line)

    def test_no_title_or_label_inside_the_fragment(self):
        """片段里不能自带【】：它只是事实块里的一句，不是一段带标题的内容。"""
        for line in life_log.render_lines([item("甲")], NOW):
            self.assertNotIn("【", line)
            self.assertNotIn("】", line)

    def test_who_falls_back_to_ta(self):
        self.assertIn("TA", life_log.render_lines([item("甲")], NOW, name_of=lambda u: "")[0])
        self.assertIn("TA", life_log.render_lines([item("甲")], NOW, name_of=lambda u: None)[0])

        def boom(uid):
            raise RuntimeError("查名字炸了")

        self.assertIn("TA", life_log.render_lines([item("甲")], NOW, name_of=boom)[0])

    def test_no_uid_does_not_invent_a_person(self):
        line = life_log.render_lines([{"text": "甲", "uid": "", "at": NOW}], NOW)[0]
        self.assertTrue(line.startswith("她手上"), line)
        self.assertNotIn("和", line, "不知道跟谁有关就别硬套一个人称")

    def test_max_open_caps_pieces(self):
        lines = life_log.render_lines([item(f"第{i}件") for i in range(6)], NOW, max_open=3)
        self.assertEqual(len(lines), 1, "同一个人的几件事合成一句")
        self.assertEqual(len(lines[0].split("：", 1)[1].split("、")), 3, "最多三件事")


class NoTitledSectionTest(unittest.TestCase):
    """这一段是**事实块里的一句**，不是 system_prompt 里的一段带标题内容。

    之前的写法是 `【她心里还搁着的事】` + 逐行正文，追加进 `req.system_prompt`。用户
    当场否掉了：开头一个标题然后是文本，最容易让模型像念稿子。所以这里把它钉死——
    标题常量与追加函数都不许回来。
    """

    def test_prompt_builder_has_no_life_mark(self):
        import humanoid.prompt_builder as pb

        self.assertFalse(hasattr(pb, "LIFE_MARK"), "不许再给这一段单独起标题")
        self.assertFalse(hasattr(pb, "build_life_note"), "不许再单独成段")
        self.assertEqual(pb.MARK_VERSION, "v12", "事实块的格式没变，版本号不该跟着动")

    def test_main_has_no_append_life(self):
        self.assertFalse(hasattr(main_module, "_append_life"), "不许再往 system_prompt 追加这一段")

    def test_core_has_no_life_note(self):
        h = _Harness()
        try:
            self.assertFalse(hasattr(h.core, "life_note"), "渲染交给 PromptBuilder，Core 只留账本")
            self.assertTrue(hasattr(h.core, "life_items"))
        finally:
            h.store.flush_sync()


class _Harness:
    """一个带 data_root 的最小可运行实例：注入链路要真的跑起来才看得出问题。"""

    def __init__(self, raw: dict | None = None) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "plugin_data" / "humanoid_core").mkdir(parents=True)
        self.box = ConfigBox(raw or {"timezone_city": "北京"})
        self.log = RecordingLogger()
        self.store = StateStore(
            self.root / "plugin_data" / "humanoid_core" / "state.json", lambda: 1.0, self.log
        )
        self.store.load(FROZEN.strftime("%Y-%m-%d"), self.box.value.cycle_length)
        ctx = FakeContext()
        resolver = ProviderResolver(ctx, self.log)
        gateway = LLMGateway(resolver, self.box, self.log)
        self.roles = RoleManager(
            self.store, self.box, self.log, resolver, gateway, None, data_root=self.root
        )
        self.engine = HumanoidEngine(ctx, self.box, self.root, self.log, None, self.roles)
        self.core = self.roles.get_or_create("bot1")
        clock = FrozenClock(FROZEN)
        self.core.clock = clock
        for service in (
            self.core.schedule, self.core.soma, self.core.energy,
            self.core.mood, self.core.social, self.core.weather,
        ):
            service._clock = clock

    def write_signals(self, payload: dict, age: float = 0.0) -> Path:
        social = self.root / "plugin_data" / SOCIAL_PLUGIN_DIR_NAME
        social.mkdir(parents=True, exist_ok=True)
        path = social / SIGNALS_FILE_NAME
        path.write_text(json.dumps(payload), encoding="utf-8")
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def publish(self, *texts: str) -> None:
        self.write_signals({"by_bid": {"bot1": {"open_items": [
            {"text": t, "uid": "10001", "at": self.core.now_epoch(), "kind": "loop"} for t in texts
        ]}}})
        self.core._apply_social_signals()

    def block(self) -> str:
        """本轮那份事实块（注入正文）。"""
        return self.core.build_injection("10001")


class OpenItemsIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _Harness()
        self.core = self.h.core

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def test_published_items_reach_the_facts_block(self):
        self.h.publish("他上次说的那个面试")
        block = self.h.block()
        self.assertIn("他上次说的那个面试", block)
        self.assertIn("还有件事没完", block)

    def test_an_item_stops_when_it_stops_being_published(self):
        self.h.publish("甲", "乙")
        self.assertIn("甲", self.h.block())
        self.h.publish("乙")
        block = self.h.block()
        self.assertNotIn("甲", block, "不再发布的那条该算结了")
        self.assertIn("乙", block)

    def test_stale_signals_do_not_wipe_the_ledger(self):
        """社交层停了：文件过期读回来是空，但那个空表示「读不到」，不是「全结了」。"""
        self.h.publish("甲")
        self.h.write_signals({"by_bid": {"bot1": {"open_items": []}}}, age=10 ** 6)
        self.core._apply_social_signals()
        self.assertIn("甲", self.h.block())

    def test_missing_signals_do_not_wipe_the_ledger(self):
        path = self.h.write_signals({"by_bid": {"bot1": {"open_items": [
            {"text": "甲", "uid": "10001", "at": self.core.now_epoch()}
        ]}}})
        self.core._apply_social_signals()
        self.assertIn("甲", self.h.block())
        os.remove(path)
        self.core._apply_social_signals()
        self.assertIn("甲", self.h.block())

    def test_a_corrupt_signals_file_does_not_wipe_the_ledger(self):
        """文件在、mtime 也新鲜，但内容读不出来：空表表示「我不知道」，不是「全结了」。

        `status()` 以前只看「payload 空不空 + mtime 过没过期」，一份坏掉的 JSON
        会被判成 `fresh`，于是这份空表被当成「写方说没有了」去整体替换她的账本——
        社交层写出半个文件，她记着的事就没了。
        """
        self.h.publish("甲")
        path = self.h.write_signals({"by_bid": {"bot1": {"open_items": []}}})
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.core._apply_social_signals()
        self.assertIn("甲", self.h.block())

    def test_soma_switch_does_not_gate_the_ledger(self):
        """账本记的不是身体状态，是「跨轮还记得什么」：关掉身体层不该连记忆一起失去。"""
        h = _Harness({"timezone_city": "北京", "soma_enabled": False})
        try:
            self.assertFalse(h.core.config.soma_enabled)
            h.publish("甲")
            self.assertIn("甲", h.block())
        finally:
            h.store.flush_sync()

    def test_no_ledger_means_no_fragment(self):
        self.assertNotIn("还有件事没完", self.h.block())

    def test_single_bot_layout_is_read_from_the_top_level(self):
        self.h.write_signals({"open_items": [
            {"text": "顶层写的", "uid": "10001", "at": self.core.now_epoch()}
        ]})
        self.core._apply_social_signals()
        self.assertIn("顶层写的", self.h.block())


class FactsBlockShapeTest(unittest.TestCase):
    """它必须长成「事实块里的一句」，而不是「又一段带标题的文本」。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.core = self.h.core

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def test_block_has_no_section_marks(self):
        self.h.publish("甲")
        block = self.h.block()
        self.assertNotIn("【", block, "事实块里不许出现第二段标题")
        self.assertNotIn("】", block)

    def test_fragment_is_comma_joined_not_on_its_own_line(self):
        """`_sentence` 用「，」把片段拼进同一句，所以正文里不该出现换行分节。"""
        self.h.publish("甲")
        body = self.h.block().split("\n", 1)[1]
        self.assertNotIn("\n", body, "事实块正文是一整句，不该有换行")

    def test_present_in_all_three_modes(self):
        for mode in ("mood_only", "medium", "full"):
            h = _Harness({"timezone_city": "北京", "inject_activity_context": mode})
            try:
                h.publish("他上次说的那个面试")
                self.assertIn("他上次说的那个面试", h.block(), f"{mode} 档丢了这一句")
            finally:
                h.store.flush_sync()

    def test_rendered_fragment_is_stable(self):
        self.h.publish("甲", "乙")
        items = self.core.life_items()
        first = life_log.render_lines(items, self.core.now_epoch())
        second = life_log.render_lines(self.core.life_items(), self.core.now_epoch())
        self.assertEqual(first, second)

    def test_no_forbidden_words(self):
        self.h.publish("他上次说的那个面试")
        block = self.h.block()
        for word in FORBIDDEN:
            self.assertNotIn(word, block, f"注入里出现了指令词「{word}」")


class StateNoteOngoingTest(unittest.TestCase):
    """`ongoing_tasks` 以前只被送去社交侧，从没进过普通对话的注入。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.core = self.h.core

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def _set_ongoing(self, *whats: str) -> None:
        self.core.schedule._scope.set_self("ongoing_tasks", [
            {"what": w, "at": self.core.now_epoch()} for w in whats
        ])

    def test_carried_into_the_state_note(self):
        self._set_ongoing("整理那份个案材料")
        note = main_module._state_note(self.core, "10001")
        self.assertIn("整理那份个案材料", note)
        self.assertIn(STATE_MARK, note)

    def test_absent_ongoing_leaves_no_dangling_separator(self):
        self._set_ongoing()
        note = main_module._state_note(self.core, "10001")
        self.assertNotIn("手上还没做完的事", note)
        self.assertNotIn("；；", note)

    def test_expired_ongoing_is_not_carried(self):
        """`ongoing_tasks` 自己的 TTL 还在把关：4 天前报过、之后没再报的算做完了。"""
        self.core.schedule._scope.set_self("ongoing_tasks", [
            {"what": "早就做完的事", "at": self.core.now_epoch() - 5 * 86400}
        ])
        self.assertNotIn("早就做完的事", main_module._state_note(self.core, "10001"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
