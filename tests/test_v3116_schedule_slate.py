"""v3.1.16 日程重构：给已校验的候选让她「选着写」，加硬准入与降级兜底。

线上老毛病（`schedule.py` 的 `_DEVICE_SWAPS` 那一代）：模型写「用蹄子夹着手机」，
替换器换成「夹着手边的东西」；这句存进日程又被注入回提示词，模型看到含糊词自己
补回「手机」，下次再被换掉——替换器在制造它想消灭的东西，还把句子改成病句。

v3.1.16 换思路：**不在生成后换词，改成生成前给候选、生成后判定**。

* `schedule_content.build_slate`：把素材/范例里过筛的候选摆出来（不带设备词、
  不带含糊词、不跟今天撞），模型可以 `"pick": 编号` 直接用；
* 生成后 `content_issues` 判设备词/含糊词 → 重写一次 → 仍脏就降级成处境词；
* 最后一道硬闸在写状态前再验一次，**不看质量闸开关**。

本文件测的就是这条链路上的每一个检查点，外加「循环很多轮也一次都不许脏」。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import CallGate, LLMGateway, ProviderResolver
from humanoid.services.schedule import (
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    ScheduleService,
    parse_segment,
)
from humanoid.services.schedule_content import (
    SLATE_MAX,
    build_slate,
    clean_slots,
    content_issues,
    describe_issues,
    fix_hint,
    is_clean,
    render_slate,
    slate_block,
)

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg, seg_reply

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)

DIRTY_EVENTS = (
    "窝在沙发上刷手机，什么正事也没做",
    "用蹄子夹着手机给姐姐发消息",
    "盯着屏幕等回音",
    "把手边的东西拆了又装回去",
)


def dirty_reply(event: str = DIRTY_EVENTS[0], minutes: int = 40) -> str:
    return json.dumps(
        {
            "continue": False,
            "event": event,
            "location": "客厅",
            "emotion": "有点空",
            "energy_rate": -0.02,
            "minutes": minutes,
        },
        ensure_ascii=False,
    )


def pick_reply(index: int, minutes: int = 40) -> str:
    return json.dumps(
        {"pick": index, "emotion": "专注", "energy_rate": -0.02, "minutes": minutes},
        ensure_ascii=False,
    )


class QueuedProvider(FakeProvider):
    """按顺序吐预设文本：第一次生成、第二次重写各用一段。"""

    def __init__(self, provider_id: str, texts, review_reply: str = '{"ok": true}') -> None:
        super().__init__(provider_id, reply=texts[0], review_reply=review_reply)
        self.texts = list(texts)

    async def text_chat(self, prompt: str | None = None, **kwargs):
        review = bool(prompt) and "【审查这一段】" in prompt
        if not review:
            index = min(self.calls, len(self.texts) - 1)
            self.reply = self.texts[index]
        return await super().text_chat(prompt=prompt, **kwargs)


def slate_events(prompt: str) -> list[str]:
    """从 prompt 的【可以选的事】里抠出候选事件名（测试用，不复制实现里的字面量）。"""
    marker = "【可以选的事】"
    if marker not in prompt:
        return []
    block = prompt.split(marker, 1)[1]
    block = block.split("\n\n", 1)[0]
    events: list[str] = []
    for line in block.splitlines():
        text = line.strip()
        if not text or not text[0].isdigit():
            continue
        body = text.split(".", 1)[-1].strip()
        events.append(body.split("（", 1)[0].strip())
    return events


class ContentIssuesTest(unittest.TestCase):
    def test_device_words_are_reported(self):
        self.assertIn("设备:手机", content_issues("窝在沙发上刷手机"))
        self.assertIn("设备:屏幕", content_issues("盯着屏幕等回音"))
        self.assertIn("设备:打字", content_issues("在窗边打字"))

    def test_vague_placeholders_are_reported(self):
        self.assertEqual(content_issues("把手边的东西拆了又装回去"), ["含糊:手边"])

    def test_plain_word_message_is_not_banned(self):
        """「消息」这个词本身不禁——禁的是「发消息」这种设备动作。"""
        self.assertEqual(content_issues("跟姐姐说了会儿话"), [])
        self.assertEqual(content_issues("把攒着的几条消息回完"), [])
        self.assertIn("设备:回消息", content_issues("把攒着的几条消息回完先回消息"))

    def test_clean_text_and_empty(self):
        self.assertTrue(is_clean("把毛线绕成团"))
        self.assertEqual(content_issues(""), [])
        self.assertTrue(is_clean(""))

    def test_describe_and_fix_hint(self):
        issues = content_issues("窝在沙发上刷手机，手边的东西还没收")
        self.assertIn("手机", describe_issues(issues))
        hint = fix_hint(issues)
        self.assertIn("她的世界没有手机", hint)
        self.assertIn("书", hint)
        self.assertEqual(fix_hint([]), "")

    def test_clean_slots_filters_without_rewriting(self):
        slots = [
            {"event": "把被子抱出去晒"},
            {"event": "窝在沙发上刷手机"},
            {"event": "把毛线绕成团"},
        ]
        kept = clean_slots(slots)
        self.assertEqual([slot["event"] for slot in kept], ["把被子抱出去晒", "把毛线绕成团"])


class BuildSlateTest(unittest.TestCase):
    def test_dirty_candidates_are_dropped(self):
        items = build_slate(
            [
                {"event": "把被子抱出去晒", "location": "阳台", "minutes": 20},
                {"event": "窝在沙发上刷手机", "location": "客厅", "minutes": 40},
                {"event": "把手边的东西拆了又装回去", "location": "书桌前", "minutes": 60},
            ]
        )
        self.assertEqual([item["event"] for item in items], ["把被子抱出去晒"])

    def test_todays_repeat_is_dropped(self):
        items = build_slate(
            [
                {"event": "把书桌上的东西擦了一遍", "location": "书房", "minutes": 30},
                {"event": "把碗洗了", "location": "厨房", "minutes": 20},
            ],
            recent_events=["把书桌上的东西擦干净了"],
        )
        self.assertEqual([item["event"] for item in items], ["把碗洗了"])

    def test_suspended_comes_first(self):
        items = build_slate(
            [{"event": "把被子抱出去晒", "location": "阳台", "minutes": 20}],
            suspended={"event": "靠着台面择菜", "location": "厨房", "remaining_minutes": 20},
        )
        self.assertEqual(items[0]["event"], "靠着台面择菜")
        self.assertEqual(items[0]["source"], "resume")
        self.assertEqual([item["id"] for item in items], list(range(1, len(items) + 1)))

    def test_dirty_suspended_is_dropped(self):
        items = build_slate(
            [{"event": "把被子抱出去晒", "location": "阳台", "minutes": 20}],
            suspended={"event": "摸出手机看了一眼", "remaining_minutes": 20},
        )
        self.assertEqual([item["event"] for item in items], ["把被子抱出去晒"])

    def test_limit_is_respected(self):
        materials = [
            {"event": f"把第{i}个小盒子擦干净", "location": "书桌前", "minutes": 20}
            for i in range(SLATE_MAX + 4)
        ]
        self.assertEqual(len(build_slate(materials)), SLATE_MAX)

    def test_render_and_block_are_readable(self):
        items = build_slate([{"event": "把被子抱出去晒", "location": "阳台", "minutes": 20}])
        rendered = render_slate(items)
        self.assertIn("把被子抱出去晒", rendered)
        self.assertIn("20 分钟", rendered)
        block = slate_block(items)
        self.assertIn("【可以选的事】", block)
        self.assertIn("pick", block)
        self.assertIn("不碰手机电脑", block)

    def test_empty_slate_is_empty(self):
        self.assertEqual(slate_block([]), "")


class ParsePickTest(unittest.TestCase):
    def test_pick_uses_the_candidate(self):
        slate = build_slate([{"event": "把毛线绕成团", "location": "窗边", "minutes": 40}])
        slot = parse_segment(
            {"pick": 1, "emotion": "专注", "energy_rate": -0.02},
            now_minute=600,
            step=5,
            slate=slate,
        )
        self.assertEqual(slot["event"], "把毛线绕成团")
        self.assertEqual(slot["location"], "窗边")
        self.assertEqual(slot["emotion"], "专注")
        self.assertEqual((slot["start"], slot["end"]), ("10:00", "10:40"))

    def test_explicit_event_wins_over_pick(self):
        slate = build_slate([{"event": "把毛线绕成团", "location": "窗边", "minutes": 40}])
        slot = parse_segment(
            {"pick": 1, "event": "把碗洗了", "minutes": 30},
            now_minute=600,
            step=5,
            slate=slate,
        )
        self.assertEqual(slot["event"], "把碗洗了")

    def test_pick_out_of_range_is_ignored(self):
        slate = build_slate([{"event": "把毛线绕成团", "location": "窗边", "minutes": 40}])
        slot = parse_segment(
            {"pick": 99, "event": "把碗洗了", "minutes": 30},
            now_minute=600,
            step=5,
            slate=slate,
        )
        self.assertEqual(slot["event"], "把碗洗了")

    def test_pick_without_slate_is_ignored(self):
        slot = parse_segment(
            {"pick": 1, "event": "把碗洗了", "minutes": 30},
            now_minute=600,
            step=5,
        )
        self.assertEqual(slot["event"], "把碗洗了")


class SlatePromptTest(unittest.IsolatedAsyncioTestCase):
    def build(self, provider, **overrides):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
            **overrides,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(FIXED_MOMENT),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, conf

    async def test_prompt_carries_a_filtered_slate(self):
        provider = FakeProvider("p", reply=seg_reply(event="把被子抱出去晒", minutes=40))
        service, _conf = self.build(provider)
        await service.ensure_fresh()
        prompt = str(provider.last_kwargs.get("prompt") or "")
        self.assertIn("【可以选的事】", prompt)
        self.assertIn("pick", prompt)
        events = slate_events(prompt)
        self.assertTrue(events, "候选清单不该是空的")
        for event in events:
            self.assertEqual(content_issues(event), [], f"候选里混进了脏东西：{event}")
        self.assertIn("手边的东西", prompt, "规则里要明说别用含糊词")

    async def test_pick_is_written_into_the_schedule(self):
        provider = FakeProvider("p", reply=pick_reply(2))
        service, _conf = self.build(provider)
        await service.ensure_fresh()
        prompt = str(provider.last_kwargs.get("prompt") or "")
        events = slate_events(prompt)
        self.assertGreaterEqual(len(events), 2)
        segs = service.segments()
        self.assertEqual(segs[0]["event"], events[1], "选中的那条应该原样落库")
        self.assertEqual(service.source, SOURCE_LLM)


class HardGateTest(unittest.IsolatedAsyncioTestCase):
    """脏事件的三条出路：重写干净 / 重写还脏就降级 / 关掉质量闸也照样拦。"""

    def build(self, provider, **overrides):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
            **overrides,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(FIXED_MOMENT),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, conf

    async def test_rewrite_turns_a_dirty_segment_clean(self):
        provider = QueuedProvider(
            "p", [dirty_reply(DIRTY_EVENTS[0]), seg_reply(event="把被子抱出去晒", minutes=40)]
        )
        service, _conf = self.build(provider)
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 2, "脏一次就该重写一次")
        segs = service.segments()
        self.assertEqual(segs[0]["event"], "把被子抱出去晒")
        self.assertEqual(service.source, SOURCE_LLM)

    async def test_still_dirty_after_rewrite_degrades_to_a_candidate(self):
        provider = PromptRecordingProvider("p", [dirty_reply(DIRTY_EVENTS[1])])
        service, _conf = self.build(provider)
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 2, "只重写一次，不无限循环")
        segs = service.segments()
        self.assertEqual(len(segs), 1)
        event = str(segs[0]["event"])
        self.assertEqual(content_issues(event), [], f"降级后仍然脏：{event}")
        self.assertFalse(segs[0].get("status"), "降级优先给具体的事，不是处境词")
        candidates = slate_events(
            next(p for p in provider.prompts if "【可以选的事】" in p)
        )
        self.assertIn(event, candidates, "兜底事件必须来自过筛的候选清单")
        self.assertEqual(service.source, SOURCE_TEMPLATE)

    async def test_final_gate_holds_without_any_switch(self):
        """设备词进日程是产品约束：v3.1.28 起没有开关可关，硬闸照样拦。"""
        provider = FakeProvider("p", reply=dirty_reply(DIRTY_EVENTS[2]))
        service, _conf = self.build(provider)
        await service.ensure_fresh()
        self.assertGreaterEqual(provider.calls, 1)
        segs = service.segments()
        self.assertEqual(content_issues(str(segs[0]["event"])), [], "最后一道硬闸必须拦住")
        self.assertEqual(service.source, SOURCE_TEMPLATE)

    async def test_clean_segment_is_untouched(self):
        provider = FakeProvider("p", reply=seg_reply(event="把毛线绕成团", minutes=40))
        service, _conf = self.build(provider)
        await service.ensure_fresh()
        segs = service.segments()
        self.assertEqual(segs[0]["event"], "把毛线绕成团")
        self.assertEqual(service.source, SOURCE_LLM)


class LoopStressTest(unittest.IsolatedAsyncioTestCase):
    """用户要的那条：**一次成功不算数**，反复循环也得次次干净。"""

    def build(self, provider):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(FIXED_MOMENT),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, conf

    async def test_twenty_rounds_never_let_a_dirty_segment_through(self):
        for round_no in range(20):
            moment = FIXED_MOMENT.replace(
                hour=9 + (round_no * 40) // 60, minute=(round_no * 40) % 60
            )
            provider = FakeProvider("p", reply=dirty_reply(DIRTY_EVENTS[round_no % len(DIRTY_EVENTS)]))
            conf = cfg(
                schedule_provider_name="p",
                schedule_min_interval_minutes=0,
                schedule_max_interval_minutes=0,
                llm_idle_silence_minutes=0,
                llm_daily_call_budget=100,
            )
            store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
            store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
            log = RecordingLogger()
            resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
            gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
            service = ScheduleService(
                store.scope, lambda: conf, FrozenClock(moment), logger=log, monotonic=time.monotonic
            )
            service.set_resolver_gateway(resolver, gateway)
            await service.ensure_fresh()
            segs = service.segments()
            self.assertTrue(segs, f"第 {round_no + 1} 轮没排出日程")
            for slot in segs:
                event = str(slot.get("event") or "")
                self.assertEqual(
                    content_issues(event), [], f"第 {round_no + 1} 轮漏了一条脏日程：{event}"
                )

    async def test_stateful_rounds_stay_clean(self):
        provider = FakeProvider("p", reply=dirty_reply(DIRTY_EVENTS[3]))
        service, _conf = self.build(provider)
        for _ in range(8):
            await service.ensure_fresh(force=True)
            for slot in service.segments():
                event = str(slot.get("event") or "")
                self.assertEqual(content_issues(event), [], f"漏了一条脏日程：{event}")
            service._clock.advance(minutes=61)


    async def test_a_whole_day_of_dirty_replies_stays_clean(self):
        """整天每一段都只吐脏句子：一天下来日程里也不许留一句脏的。"""
        provider = FakeProvider("p", reply=dirty_reply(DIRTY_EVENTS[0]))
        service, _conf = self.build(provider)
        for _ in range(24):
            await service.ensure_fresh(force=True)
            for slot in service.segments():
                event = str(slot.get("event") or "")
                self.assertEqual(content_issues(event), [], f"漏了一条脏日程：{event}")
                self.assertTrue(event.strip(), "不许落空事件")
            service._clock.advance(minutes=61)
        self.assertGreater(
            service._quality["fallback"], 0, "整天都吐脏句子，闸门一次都没触发，测试没测到东西"
        )
        self.assertGreater(
            service._quality["content"], 0, "整天都吐脏句子，内容闸一次都没拦到"
        )


class ReadPathFilterTest(unittest.IsolatedAsyncioTestCase):
    """旧版本已经写脏的日程：不回提示词、不当当前活动、不拿来 resume。"""

    def build(self, provider):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(FIXED_MOMENT),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service

    async def test_dirty_history_is_not_fed_back_to_prompt(self):
        provider = FakeProvider("p", reply=seg_reply(event="把毛线绕成团", minutes=40))
        service = self.build(provider)
        service._scope.set_self("today_date", FIXED_TODAY)
        service._scope.set_self(
            "daily_schedule",
            [
                {
                    "start": "08:00",
                    "end": "08:40",
                    "event": "窝在沙发上刷手机",
                    "location": "客厅",
                    "emotion": "空",
                }
            ],
        )
        await service.ensure_fresh(force=True)
        prompt = str(provider.last_kwargs.get("prompt") or "")
        self.assertNotIn("刷手机", prompt, "旧脏日程又被喂回提示词了")

    async def test_dirty_active_segment_is_not_shown_as_activity(self):
        service = self.build(FakeProvider("p", reply=seg_reply(minutes=40)))
        service._scope.set_self("today_date", FIXED_TODAY)
        service._scope.set_self(
            "daily_schedule",
            [
                {
                    "start": "08:30",
                    "end": "09:30",
                    "event": "盯着屏幕等回音",
                    "location": "客厅",
                    "emotion": "空",
                }
            ],
        )
        self.assertEqual(service.current_activity(), {}, "脏段不该被当成她正在做的事")
        self.assertNotIn("屏幕", str(service.current_slot().get("event") or ""))

    async def test_dirty_suspended_is_dropped_and_cleared(self):
        service = self.build(FakeProvider("p", reply=seg_reply(minutes=40)))
        service._scope.set_self(
            "schedule_suspended",
            {
                "event": "用蹄子夹着手机给姐姐发消息",
                "location": "客厅",
                "at": 0.0,
                "ttl_minutes": 120,
                "remaining_minutes": 30,
            },
        )
        self.assertEqual(service._suspended_slot(), {}, "旧脏挂起不许拿来 resume")
        self.assertIsNone(service._scope.get_self("schedule_suspended"), "脏挂起该被清掉")


class OtherSinksTest(unittest.IsolatedAsyncioTestCase):
    """日程文本还有几个出口：梦、跨天任务、日框、/查看日程、状态页。都要过同一道筛。"""

    def build(self, provider=None):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = provider or FakeProvider("p", reply=seg_reply(minutes=40))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(FIXED_MOMENT),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service

    def seed(self, service, slots):
        service._scope.set_self("today_date", FIXED_TODAY)
        service._scope.set_self("daily_schedule", slots)

    def test_dirty_dreams_are_dropped(self):
        from humanoid.services.schedule import _clean_dreams

        dreams = _clean_dreams(["梦见手机亮了又暗", "梦见一只猫蹲在窗台", "梦见手边的东西被人收走了"])
        self.assertEqual(dreams, ["一只猫蹲在窗台"])

    def test_dirty_ongoing_is_not_recorded(self):
        service = self.build()
        service._note_ongoing({"event": "整理旧信", "ongoing": "用蹄子夹着手机在记"})
        self.assertEqual(service.ongoing_tasks(), [], "脏的跨天任务不许记下来")

    def test_clean_ongoing_still_works(self):
        service = self.build()
        service._note_ongoing({"event": "整理旧信", "ongoing": "还剩三封没读完"})
        self.assertEqual(service.ongoing_tasks(), ["还剩三封没读完"])

    def test_dirty_day_frame_falls_back(self):
        service = self.build()
        service._store_day_frame("一整天都在刷手机")
        frame = service._cached_day_frame()
        self.assertEqual(content_issues(frame), [], f"日框还是脏的：{frame}")

    def test_display_current_slots_and_active_skip_dirty(self):
        service = self.build()
        self.seed(
            service,
            [
                {
                    "start": "07:00",
                    "end": "07:40",
                    "event": "窝在沙发上刷手机",
                    "location": "客厅",
                },
                {
                    "start": "07:40",
                    "end": "08:30",
                    "event": "把桌上的小盒子拆了又装回去",
                    "location": "书桌前",
                },
                {
                    "start": "08:30",
                    "end": "09:30",
                    "event": "盯着屏幕等回音",
                    "location": "客厅",
                },
            ],
        )
        self.assertEqual(
            [s["event"] for s in service.display_segments()],
            ["把桌上的小盒子拆了又装回去"],
            "/查看日程不该列出旧脏段",
        )
        self.assertEqual(
            [s["event"] for s in service.current_slots()],
            ["把桌上的小盒子拆了又装回去"],
            "契约/社交层拿到的也不该有旧脏段",
        )
        self.assertIsNone(service.active_segment(), "脏段不算她此刻在做的事")
        self.assertEqual(content_issues(str(service.status().get("active") or "")), [])

    async def test_dirty_dream_from_model_never_lands(self):
        provider = FakeProvider(
            "p",
            reply=json.dumps(
                {
                    "continue": False,
                    "event": "睡了一觉",
                    "location": "卧室",
                    "emotion": "松",
                    "energy_rate": 0.1,
                    "minutes": 60,
                    "sleep": True,
                    "dreams": ["梦见手机屏幕亮着", "梦见雨落在窗台上"],
                },
                ensure_ascii=False,
            ),
        )
        service = self.build(provider)
        await service.ensure_fresh(force=True)
        dreams = service.last_dreams()
        self.assertEqual(dreams, ["雨落在窗台上"])


class PromptRecordingProvider(QueuedProvider):
    """把每次送出去的 prompt 记下来，好核对候选清单。"""

    def __init__(self, provider_id: str, texts, review_reply: str = '{"ok": true}') -> None:
        super().__init__(provider_id, texts, review_reply)
        self.prompts: list[str] = []

    async def text_chat(self, prompt: str | None = None, **kwargs):
        self.prompts.append(str(prompt or ""))
        return await super().text_chat(prompt=prompt, **kwargs)


class DegradeToSlateTest(unittest.IsolatedAsyncioTestCase):
    """降级不再只会说「安静待着」：优先用筛过的具体候选顶上。"""

    def build(self, provider, moment=FIXED_MOMENT):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, conf

    async def test_dirty_twice_degrades_to_a_concrete_candidate(self):
        provider = PromptRecordingProvider(
            "p", [dirty_reply(DIRTY_EVENTS[0]), dirty_reply(DIRTY_EVENTS[1])]
        )
        service, _ = self.build(provider)
        await service.ensure_fresh(force=True)
        segs = service.segments()
        self.assertTrue(segs, "降级之后也得有段")
        slot = segs[-1]
        self.assertEqual(content_issues(str(slot["event"])), [])
        self.assertFalse(slot.get("status"), "该拿具体的事兜底，不是处境词")
        prompts = [p for p in provider.prompts if "【可以选的事】" in p]
        self.assertTrue(prompts)
        candidates = slate_events(prompts[0])
        self.assertIn(slot["event"], candidates, "兜底事件必须来自过筛的候选清单")

    def test_no_candidates_falls_back_to_status_word(self):
        service, conf = self.build(PromptRecordingProvider("p", [dirty_reply()]))
        slot = service._degrade_to_neutral(conf, 9 * 60, None, ())
        self.assertTrue(slot.get("status"))

    def test_degrade_avoids_the_previous_event(self):
        service, conf = self.build(PromptRecordingProvider("p", [dirty_reply()]))
        candidates = build_slate(
            [{"event": "把晒干的衣服收进来", "location": "阳台", "minutes": 40}],
            [{"event": "把桌上的小盒子拆了又装回去", "location": "书桌前", "minutes": 45}],
        )
        first = candidates[0]["event"]
        slot = service._degrade_to_neutral(conf, 9 * 60, {"event": first}, candidates)
        self.assertNotEqual(slot["event"], first)


    def test_night_slate_only_offers_night_things(self):
        """候选是按钟点筛的：凌晨的候选里不该出现白天的事，降级自然不会顶错。"""
        from humanoid.services.life_material import materials_for

        night = [
            item["event"] for item in materials_for(["role", FIXED_TODAY], now_minute=3 * 60 + 10)
        ]
        self.assertTrue(night)
        self.assertNotIn("把晒干的衣服收进来", night)


class SlateRejectsVagueTest(unittest.TestCase):
    """候选必须是能看见的动作（v3.1.27）。

    线上症状：素材池里「什么也不干，坐着发了会儿呆」被自愈从 slate 挑中直接落库，
    读者还是不知道她在做什么。所以 slate 这一层就要把空话挡掉。
    """

    def test_low_info_material_never_enters_the_slate(self):
        items = build_slate(
            [
                {"event": "什么也不干，坐着发了会儿呆", "location": "客厅", "minutes": 30},
                {"event": "把碗洗了，灶台擦了一遍", "location": "厨房", "minutes": 20},
            ]
        )
        events = [item["event"] for item in items]
        self.assertEqual(events, ["把碗洗了，灶台擦了一遍"])

    def test_low_info_suspended_item_is_not_offered(self):
        items = build_slate(
            materials=[{"event": "去楼下买了把青菜", "location": "菜场", "minutes": 25}],
            suspended={"event": "忙手里的活", "location": "家里", "remaining_minutes": 40},
        )
        self.assertEqual([item["event"] for item in items], ["去楼下买了把青菜"])


if __name__ == "__main__":
    unittest.main()
