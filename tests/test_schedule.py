"""日程：滚动分段生成、概率决策、跨夜结转、退避与单飞、时段规范化。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from humanoid.clock import Clock
from humanoid.config import HumanoidConfig
from humanoid.jsonx import extract_json_array, extract_json_object
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.process import ProcessService
from humanoid.services.schedule import (
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    SEGMENT_MIN_MINUTES,
    ScheduleService,
    dynamic_segment,
    parse_segment,
    segment_prompt,
)
from humanoid.slots import (
    DAY_MINUTES,
    coverage_is_complete,
    find_slot,
    format_time,
    normalize_slots,
    parse_time,
    sleep_window_minutes,
)
from humanoid.state import StateStore

from .fakes import FakeClock, FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def cfg(**overrides) -> HumanoidConfig:
    # chance 默认压到 0：概率重估单独测，不让它污染「不该生成」的断言
    return HumanoidConfig.from_raw({"timezone_city": "北京", "schedule_change_chance": 0, **overrides})


def today() -> str:
    """与状态文件同一天：调度服务靠日期比较判断跨天，测试里两边必须一致。"""
    return FIXED_TODAY


def seg_reply(event="去超市买菜", minutes=85, **extra) -> str:
    payload = {
        "continue": False,
        "event": event,
        "location": "超市",
        "emotion": "随性",
        "energy_rate": -0.05,
        "minutes": minutes,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def continue_reply(minutes=60) -> str:
    return json.dumps({"continue": True, "minutes": minutes}, ensure_ascii=False)


class SlotNormalizeTest(unittest.TestCase):
    def test_parse_and_format_roundtrip(self):
        self.assertEqual(parse_time("07:30"), 450)
        self.assertEqual(parse_time("24:00"), DAY_MINUTES)
        self.assertEqual(parse_time("7:5"), 425)
        self.assertEqual(parse_time("07：30"), 450)  # 全角冒号
        self.assertIsNone(parse_time("abc"))
        self.assertIsNone(parse_time("25:00"))
        self.assertIsNone(parse_time("07:99"))
        self.assertEqual(format_time(DAY_MINUTES), "24:00")
        self.assertEqual(format_time(0), "00:00")

    def test_drops_non_mapping_entries(self):
        raw = ["junk", 42, None, {"start": "00:00", "end": "24:00", "event": "宅家"}]
        slots = normalize_slots(raw)
        self.assertEqual(len(slots), 1)
        self.assertTrue(coverage_is_complete(slots))

    def test_drops_reversed_and_zero_length(self):
        raw = [
            {"start": "10:00", "end": "09:00", "event": "倒置"},
            {"start": "11:00", "end": "11:00", "event": "零长"},
            {"start": "00:00", "end": "10:00", "event": "有效"},
        ]
        slots = normalize_slots(raw)
        self.assertTrue(coverage_is_complete(slots))
        self.assertEqual(slots[0]["event"], "有效")

    def test_sorts_unordered_input(self):
        raw = [
            {"start": "12:00", "end": "24:00", "event": "下半天"},
            {"start": "00:00", "end": "12:00", "event": "上半天"},
        ]
        slots = normalize_slots(raw)
        self.assertEqual([s["event"] for s in slots], ["上半天", "下半天"])
        self.assertTrue(coverage_is_complete(slots))

    def test_trims_overlaps(self):
        raw = [
            {"start": "00:00", "end": "13:00", "event": "A"},
            {"start": "12:00", "end": "24:00", "event": "B"},
        ]
        slots = normalize_slots(raw)
        self.assertTrue(coverage_is_complete(slots))
        self.assertEqual(slots[0]["end"], "13:00")
        self.assertEqual(slots[1]["start"], "13:00")

    def test_fills_leading_middle_trailing_gaps(self):
        raw = [
            {"start": "08:00", "end": "09:00", "event": "早"},
            {"start": "20:00", "end": "21:00", "event": "晚"},
        ]
        slots = normalize_slots(raw)
        self.assertTrue(coverage_is_complete(slots))
        self.assertEqual(slots[0]["start"], "00:00")
        self.assertEqual(slots[-1]["end"], "24:00")

    def test_clamps_energy_rate(self):
        raw = [
            {"start": "00:00", "end": "12:00", "event": "A", "energy_rate": 9.0},
            {"start": "12:00", "end": "24:00", "event": "B", "energy_rate": "-9"},
        ]
        slots = normalize_slots(raw)
        self.assertEqual(slots[0]["energy_rate"], 0.3)
        self.assertEqual(slots[1]["energy_rate"], -0.3)

    def test_bad_energy_rate_becomes_zero(self):
        raw = [{"start": "00:00", "end": "24:00", "event": "A", "energy_rate": "很高"}]
        self.assertEqual(normalize_slots(raw)[0]["energy_rate"], 0.0)

    def test_caps_slot_count_by_merging(self):
        raw = [
            {"start": format_time(i * 15), "end": format_time((i + 1) * 15), "event": f"块{i}", "energy_rate": 0.1}
            for i in range(96)
        ]
        slots = normalize_slots(raw, max_slots=16)
        self.assertLessEqual(len(slots), 16)
        self.assertTrue(coverage_is_complete(slots))

    def test_aligns_to_granularity(self):
        raw = [
            {"start": "00:00", "end": "07:33", "event": "睡"},
            {"start": "07:33", "end": "24:00", "event": "醒"},
        ]
        slots = normalize_slots(raw, align_minutes=15)
        self.assertTrue(coverage_is_complete(slots))
        for slot in slots:
            for key in ("start", "end"):
                self.assertEqual(parse_time(slot[key]) % 15, 0, slot)

    def test_returns_none_for_hopeless_input(self):
        self.assertIsNone(normalize_slots([]))
        self.assertIsNone(normalize_slots(None))
        self.assertIsNone(normalize_slots("[]"))
        self.assertIsNone(normalize_slots([{"start": "x", "end": "y"}]))

    def test_find_slot_is_half_open(self):
        slots = normalize_slots(
            [{"start": "00:00", "end": "08:00", "event": "A"}, {"start": "08:00", "end": "24:00", "event": "B"}]
        )
        self.assertEqual(find_slot(slots, 0)["event"], "A")
        self.assertEqual(find_slot(slots, 479)["event"], "A")
        self.assertEqual(find_slot(slots, 480)["event"], "B")
        self.assertEqual(find_slot(slots, DAY_MINUTES)["event"], "B")


class JsonExtractionTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(extract_json_array('[1, 2]'), [1, 2])

    def test_fenced_array(self):
        raw = "这是你要的日程：\n```json\n[{\"a\": 1}]\n```\n希望有帮助"
        self.assertEqual(extract_json_array(raw), [{"a": 1}])

    def test_array_with_brackets_inside_strings(self):
        raw = '前言 [{"event": "看剧 [第2季]", "n": 1}] 后记'
        parsed = extract_json_array(raw)
        self.assertEqual(parsed, [{"event": "看剧 [第2季]", "n": 1}])

    def test_array_wrapped_in_object(self):
        raw = '{"schedule": [{"event": "睡"}]}'
        self.assertEqual(extract_json_array(raw), [{"event": "睡"}])

    def test_no_array(self):
        self.assertIsNone(extract_json_array("对不起，我做不到"))
        self.assertIsNone(extract_json_array(""))

    def test_object_extraction(self):
        raw = '好的：{"affection_delta": 1.5, "note": "含 } 的文本"}'
        self.assertEqual(
            extract_json_object(raw), {"affection_delta": 1.5, "note": "含 } 的文本"}
        )

    def test_object_none(self):
        self.assertIsNone(extract_json_object("没有对象"))


class SegmentParseTest(unittest.TestCase):
    """模型返回的这一段怎么规范化：minutes/end 两种写法、continue、跨夜、钳制。"""

    def test_minutes_reply(self):
        slot = parse_segment(
            {"event": "改海报", "minutes": 85, "location": "公司", "energy_rate": -0.1},
            now_minute=9 * 60,
            step=15,
        )
        self.assertEqual(slot["start"], "09:00")
        self.assertEqual(slot["end"], "10:25")
        self.assertEqual(slot["event"], "改海报")

    def test_start_never_rounds_into_the_future(self):
        """20:40 决定的一段，起点必须罩得住现在：向下对齐，不能四舍五入到 20:45。"""
        slot = parse_segment({"event": "看剧", "minutes": 45}, now_minute=20 * 60 + 40, step=15)
        self.assertEqual(slot["start"], "20:30")
        self.assertLess(parse_time(slot["start"]), 20 * 60 + 41)

    def test_end_style_reply(self):
        slot = parse_segment({"event": "开会", "end": "11:30"}, now_minute=10 * 60, step=15)
        self.assertEqual(slot["start"], "10:00")
        self.assertEqual(slot["end"], "11:30")

    def test_continue_reuses_previous_event(self):
        prev = {"event": "改海报", "location": "公司", "emotion": "专注", "energy_rate": -0.1}
        slot = parse_segment({"continue": True, "minutes": 60}, now_minute=10 * 60, step=15, prev=prev)
        self.assertEqual(slot["event"], "改海报")
        self.assertEqual(slot["location"], "公司")
        self.assertEqual(slot["energy_rate"], -0.1)

    def test_continue_without_previous_is_rejected(self):
        self.assertIsNone(parse_segment({"continue": True, "minutes": 60}, now_minute=600, step=15))

    def test_missing_event_is_rejected(self):
        self.assertIsNone(parse_segment({"minutes": 60}, now_minute=600, step=15))
        self.assertIsNone(parse_segment("我没办法决定", now_minute=600, step=15))

    def test_missing_duration_falls_back_to_default(self):
        slot = parse_segment({"event": "发呆"}, now_minute=600, step=15)
        self.assertEqual(parse_time(slot["end"]) - parse_time(slot["start"]), 60)

    def test_awake_duration_is_capped(self):
        slot = parse_segment({"event": "自由活动", "minutes": 9999}, now_minute=600, step=15)
        self.assertEqual(parse_time(slot["end"]), parse_time("10:00") + 180)

    def test_sleep_can_cross_midnight(self):
        """23:00 决定睡到明天 07:00：今天截到 24:00，剩下的进 carry_end。"""
        slot = parse_segment(
            {"event": "睡觉", "end": "07:00", "location": "卧室", "energy_rate": 0.15},
            now_minute=23 * 60,
            step=15,
        )
        self.assertEqual(slot["start"], "23:00")
        self.assertEqual(slot["end"], "24:00")
        self.assertEqual(slot["carry_end"], "07:00")

    def test_sleep_minutes_over_eight_hours(self):
        slot = parse_segment({"event": "睡眠", "minutes": 480}, now_minute=23 * 60, step=15)
        self.assertEqual(slot["end"], "24:00")
        self.assertEqual(slot["carry_end"], "07:00")

    def test_sleep_capped_at_twelve_hours(self):
        slot = parse_segment({"event": "睡眠", "minutes": 9999}, now_minute=12 * 60, step=15)
        self.assertEqual(parse_time(slot["end"]), 12 * 60 + 720)

    def test_min_duration_floor(self):
        slot = parse_segment({"event": "倒杯水", "minutes": 3}, now_minute=600, step=15)
        self.assertEqual(
            parse_time(slot["end"]) - parse_time(slot["start"]), SEGMENT_MIN_MINUTES
        )

    def test_array_reply_picks_covering_slot(self):
        """模型偶尔包一层数组（旧版整表习惯）：取覆盖现在的那格当这一段。"""
        raw = [
            {"start": "00:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.15},
            {"start": "08:00", "end": "18:00", "event": "工作", "energy_rate": -0.1},
            {"start": "18:00", "end": "24:00", "event": "休闲", "energy_rate": 0.05},
        ]
        slot = parse_segment(raw, now_minute=14 * 60, step=15)
        self.assertEqual(slot["event"], "工作")
        self.assertEqual(slot["end"], "17:00", "清醒段超长要被钳到 3 小时（14:00 起算）")


class SegmentPromptTest(unittest.TestCase):
    def build(self, **kw) -> str:
        return segment_prompt(
            cfg(**kw),
            now_text="14:35",
            weekday="六",
            prev=kw.pop("prev", None),
            elapsed_minutes=kw.pop("elapsed_minutes", 0),
            history=kw.pop("history", None),
            is_night=kw.pop("is_night", False),
        )

    def test_asks_for_exactly_one_segment(self):
        prompt = self.build()
        self.assertIn("从现在开始的这一段", prompt)
        self.assertIn("只这一段", prompt)
        self.assertIn("之后的事到了时候会再决定", prompt)
        self.assertIn("只输出一个 JSON 对象", prompt)
        # 整表时代的痕迹不该回来
        self.assertNotIn("00:00 开始", prompt)
        self.assertNotIn("24:00 结束", prompt)
        self.assertNotIn("首尾相连", prompt)

    def test_prev_line_and_first_decision(self):
        prompt = self.build(
            prev={"event": "改第三季度的海报", "start": "13:20", "location": "公司"},
            elapsed_minutes=75,
        )
        self.assertIn("从 13:20 开始在做「改第三季度的海报」", prompt)
        self.assertIn("已经 75 分钟", prompt)
        first = self.build()
        self.assertIn("这是今天的第一次决定", first)

    def test_history_block(self):
        history = [
            {"start": "07:10", "end": "07:40", "event": "起床洗漱", "location": "家里"},
            {"start": "07:40", "end": "12:10", "event": "改海报", "location": "公司"},
        ]
        prompt = self.build(history=history)
        self.assertIn("已经过完的时段", prompt)
        self.assertIn("07:10-07:40 起床洗漱（家里）", prompt)

    def test_body_values_are_references(self):
        body = {
            "energy": 42.0,
            "hunger": 77.0,
            "sleep_pressure": 81.0,
            "sleep_debt": 3.5,
            "last_sleep_hours": 5.5,
        }
        prompt = segment_prompt(
            cfg(), now_text="14:35", weekday="六", body=body
        )
        self.assertIn("【她此刻的身体参考数值】", prompt)
        self.assertIn("不是必须满足的条件", prompt)
        self.assertIn("精力：42", prompt)
        self.assertIn("饥饿：77", prompt)
        self.assertIn("睡眠债：3.5 小时", prompt)
        self.assertIn("当前时间：14:35", prompt)

    def test_extra_is_a_reference_not_an_order(self):
        prompt = self.build(schedule_prompt_extra="偏爱户外")
        self.assertIn("偏爱户外", prompt)
        self.assertIn("仅供参考", prompt)
        self.assertNotIn("必须", prompt.split("【可选偏好】")[1].split("【怎么决定】")[0])

    def test_empty_extra_is_omitted(self):
        self.assertNotIn("可选偏好", self.build(schedule_prompt_extra=""))

    def test_night_line(self):
        self.assertIn("生物钟夜里", self.build(is_night=True))
        self.assertNotIn("生物钟夜里", self.build(is_night=False))

    def test_sleep_duration_is_allowed_to_span_midnight(self):
        prompt = self.build()
        self.assertIn("跨过半夜也没关系", prompt)


class DynamicSegmentTest(unittest.TestCase):
    """模型不可用时的本地兜底：按身体与钟点现算，夜里睡到窗口结束。"""

    def test_night_sleep_runs_until_window_end_with_carry(self):
        cfg_night = cfg(night_start_hour=23, night_end_hour=7)
        slot = dynamic_segment(
            cfg_night, {"sleep_pressure": 80.0}, now_minute=23 * 60 + 30, step=15,
            seed=["bot", "2026-08-22"],
        )
        self.assertIn(slot["event"], ("睡一觉", "接着睡", "躺下睡"))
        self.assertEqual(slot["start"], "23:30")
        self.assertEqual(slot["end"], "24:00")
        self.assertEqual(slot["carry_end"], "07:00", "一觉跨午夜要结转到明天窗口结束")

    def test_after_midnight_sleep_until_window_end(self):
        cfg_night = cfg(night_start_hour=23, night_end_hour=7)
        slot = dynamic_segment(
            cfg_night, {"sleep_pressure": 80.0}, now_minute=2 * 60, step=15,
            seed=["bot", "2026-08-23"],
        )
        self.assertEqual(slot["start"], "02:00")
        self.assertEqual(slot["end"], "07:00")
        self.assertNotIn("carry_end", slot)

    def test_hunger_override(self):
        """饥饿越线要切到“饿”这个状态，但**不编造她具体在吃什么**。

        旧实现在这里写死了 5 条具体事件（“弄点吃的/下楼买饭团/煮碗面/叫了份外卖/
        热昨天剩的饭”），它们会变成「她今天下午在煮碗面」这样一句事实句交给模型。
        身体读数决定的是“她在什么状态”，不是“她干了什么”。
        """
        slot = dynamic_segment(
            cfg(), {"hunger": 92.0}, now_minute=15 * 60, step=15, seed=["bot", "d"],
        )
        self.assertTrue(any(w in slot["event"] for w in ("饿", "想找点吃的", "肚子空着")), slot)
        for banned in ("饭团", "面", "外卖", "煮", "热昨天"):
            self.assertNotIn(banned, slot["event"], f"又编起具体事件了：{slot['event']}")
        self.assertEqual(slot["emotion"], "饿")

    def test_night_awake_low_stays_low_stimulus(self):
        """夜里还醒着：给“还没什么困意”这类状态，地点不预设她已经在卧室。

        旧实现写死 location="卧室"（连带三条“在床上刷手机/半梦半醒/起来喝水”），
        那是替她写好了半夜在干什么。
        """
        slot = dynamic_segment(
            cfg(night_start_hour=23, night_end_hour=7),
            {"sleep_pressure": 40.0}, now_minute=1 * 60, step=15, seed=["bot", "d"],
        )
        self.assertTrue(any(w in slot["event"] for w in ("困意", "安静待着", "夜里醒着")), slot)
        for banned in ("手机", "半梦半醒", "喝水"):
            self.assertNotIn(banned, slot["event"], f"又编起具体事件了：{slot['event']}")

    def test_fallback_never_presets_an_identity(self):
        """兼容底不预设她是上班族还是学生。

        旧实现在上午/下午把地点写成“工位/书房/教室”，那等于凭空给她安一份工作。
        """
        for hour in range(24):
            for body in ({}, {"energy": 15.0}, {"hunger": 95.0}):
                slot = dynamic_segment(
                    cfg(), body, now_minute=hour * 60, step=15, seed=["bot", f"d{hour}"],
                )
                for banned in ("工位", "教室", "书房", "通勤"):
                    self.assertNotIn(
                        banned, slot.get("location", ""), f"{hour} 点给地点安了身份"
                    )
                    self.assertNotIn(banned, slot["event"], f"{hour} 点事件里安了身份")

    def test_daytime_follows_hours(self):
        slot = dynamic_segment(cfg(), {}, now_minute=10 * 60, step=15, seed=["bot", "d"])
        self.assertTrue(slot["event"])
        self.assertTrue(slot["start"] < slot["end"])


class ScheduleServiceTest(unittest.IsolatedAsyncioTestCase):
    def build(
        self,
        config=None,
        providers=None,
        global_provider=None,
        monotonic=None,
        body=None,
        random_source=None,
        moment=FIXED_MOMENT,
    ):
        conf = config or cfg()
        self.conf = conf
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=list(providers or []), global_provider=global_provider)
        resolver = ProviderResolver(ctx, log)
        clock_source = monotonic or time.monotonic
        gateway = LLMGateway(resolver, lambda: self.conf, log, clock_source)
        clock = FrozenClock(moment)
        service = ScheduleService(
            store.scope,
            lambda: self.conf,
            clock,
            logger=log,
            monotonic=clock_source,
            body_provider=body,
            random_source=random_source,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, store, log

    async def test_read_path_is_sync_and_never_touches_provider(self):
        provider = FakeProvider("p", reply=seg_reply(), delay=10.0)
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        slots = service.current_slots()  # 同步调用，绝不 await
        self.assertEqual(slots, [], "没有段就是没有段，不拿模板凑一整天")
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertEqual(provider.calls, 0, "读路径不允许调用模型")
        self.assertIsNone(store.get("today_date"), "读路径不落盘")

    async def test_current_slot_returns_something_usable(self):
        service, _, _ = self.build()
        slot = service.current_slot(minutes=13 * 60)
        self.assertIn("event", slot)
        self.assertTrue(slot["event"])

    async def test_first_segment_installs_and_covers_now(self):
        provider = FakeProvider("p", reply=seg_reply(minutes=85))
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(service.source, SOURCE_LLM)
        segs = service.segments()
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], "09:00")
        self.assertEqual(segs[0]["end"], "10:25")
        self.assertEqual(store.get("schedule_source"), SOURCE_LLM)
        activity = service.current_activity()
        self.assertEqual(activity["name"], "去超市买菜")
        self.assertEqual(activity["remaining_minutes"], 85)

    async def test_continue_extends_without_new_entry(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=60))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        service._clock.advance(minutes=30)
        provider.reply = continue_reply(minutes=90)
        self.assertTrue(await service.ensure_fresh(force=True))
        segs = service.segments()
        self.assertEqual(len(segs), 1, "接着做同一件事不该拆成两段")
        self.assertEqual(segs[0]["start"], "09:00")
        self.assertEqual(segs[0]["end"], "11:00", "09:30 决定再做 90 分钟：11:00")

    async def test_change_truncates_previous_segment(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=90))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()  # 09:00-10:30 改海报
        service._clock.advance(minutes=40)  # 09:40
        provider.reply = seg_reply(event="去公司楼下取快递", minutes=30)
        self.assertTrue(await service.ensure_fresh(force=True))
        segs = service.segments()
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start"], "09:00")
        self.assertEqual(segs[0]["end"], "09:30", "提前结束：旧段收到新段起点（09:40 向下对齐 09:30）")
        self.assertEqual(segs[1]["start"], "09:30")
        self.assertEqual(segs[1]["event"], "去公司楼下取快递")

    async def test_expired_segment_regenerates(self):
        provider = FakeProvider("p", reply=seg_reply(minutes=25))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()  # 09:00-09:25
        self.assertFalse(service.refresh_due())
        service._clock.advance(minutes=26)  # 09:26 已过完
        self.assertTrue(service.refresh_due())
        provider.reply = seg_reply(event="回消息", minutes=40)
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(len(service.segments()), 2)

    async def test_gap_after_long_downtime_is_not_backfilled(self):
        """停机几小时后回来：旧段不顺延补窟窿——那段时间她做了什么没人知道。"""
        provider = FakeProvider("p", reply=seg_reply(minutes=60))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()  # 09:00-10:00
        service._clock.advance(minutes=6 * 60)  # 15:00
        provider.reply = seg_reply(event="接着干活", minutes=60)
        self.assertTrue(await service.ensure_fresh())
        segs = service.segments()
        self.assertEqual(segs[0]["end"], "10:00", "旧段保持原样，不被拉长到 15:00")
        self.assertEqual(segs[1]["start"], "15:00")

    async def test_short_gap_is_merged(self):
        provider = FakeProvider("p", reply=seg_reply(minutes=30))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()  # 09:00-09:30
        service._clock.advance(minutes=40)  # 09:40，隔了 10 分钟
        provider.reply = seg_reply(event="回消息", minutes=30)
        self.assertTrue(await service.ensure_fresh())
        segs = service.segments()
        self.assertEqual(segs[0]["end"], "09:30", "短空档由旧段顺延补上，不留窟窿")

    async def test_sleep_segment_is_never_rerolled(self):
        provider = FakeProvider("p", reply=seg_reply(event="睡觉", minutes=480))
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_change_chance=100), [provider]
        )
        await service.ensure_fresh()  # 09:00-17:00 睡觉
        for _ in range(8):
            service._clock.advance(minutes=15)
            self.assertFalse(service.refresh_due(), "睡着的那一段不重掷、不越线打断")
        self.assertEqual(provider.calls, 1)

    async def test_roll_chance_zero_keeps_current_segment(self):
        provider = FakeProvider("p", reply=seg_reply(minutes=180))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        for _ in range(4):
            service._clock.advance(minutes=15)
            self.assertFalse(service.refresh_due(), "没到期、没报警、骰子掷不中：接着做手上的事")
        self.assertEqual(provider.calls, 1)

    async def test_roll_chance_hundred_regenerates_each_window(self):
        provider = FakeProvider("p", reply=seg_reply(minutes=180))
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_change_chance=100), [provider]
        )
        await service.ensure_fresh()
        service._clock.advance(minutes=15)
        self.assertTrue(service.refresh_due())
        provider.reply = continue_reply(minutes=60)
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 2)

    async def test_roll_result_is_stable_within_window(self):
        """同一个决策窗里掷一次就定：30 秒一查的后台循环不能反复掷。"""
        rolls = iter([0.99, 0.05, 0.05, 0.05])
        provider = FakeProvider("p", reply=seg_reply(minutes=180))
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_change_chance=30),
            [provider],
            random_source=lambda: next(rolls),
        )
        await service.ensure_fresh()
        service._clock.advance(minutes=5)
        self.assertFalse(service.refresh_due(), "第一掷没中（0.99），本窗内不再掷")
        service._clock.advance(minutes=5)
        self.assertFalse(service.refresh_due())
        service._clock.advance(minutes=6)  # 进入下一个窗
        self.assertTrue(service.refresh_due(), "新窗重掷，这次掷中（0.05）")

    async def test_hunger_override_breaks_current_segment(self):
        provider = FakeProvider("p", reply=seg_reply(event="改报表", minutes=180))
        readings = {"hunger": 40.0}

        service, _, _ = self.build(
            cfg(schedule_provider_name="p"),
            [provider],
            body=lambda: dict(readings),
        )
        await service.ensure_fresh()
        self.assertFalse(service.refresh_due())
        readings["hunger"] = 90.0
        self.assertTrue(service.refresh_due(), "饿到发慌还在改报表：当场重新决定")
        provider.reply = seg_reply(event="下楼买饭", minutes=40)
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(service.segments()[-1]["event"], "下楼买饭")

    async def test_body_values_travel_into_the_prompt(self):
        provider = FakeProvider("p", reply=seg_reply())
        readings = {"energy": 55.0, "hunger": 60.0, "sleep_pressure": 70.0, "sleep_debt": 2.0}
        service, _, _ = self.build(
            cfg(schedule_provider_name="p"), [provider], body=lambda: dict(readings)
        )
        await service.ensure_fresh()
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("【她此刻的身体参考数值】", prompt)
        self.assertIn("精力：55", prompt)
        self.assertIn("饥饿：60", prompt)
        self.assertIn("不是必须满足的条件", prompt)

        readings.update({"energy": 20.0, "hunger": 90.0})
        service._clock.advance(minutes=120)
        provider.reply = seg_reply(event="接着弄", minutes=40)
        await service.ensure_fresh(force=True)
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("精力：20", prompt, "重排时该拿到新的活值")
        self.assertIn("饥饿：90", prompt)

    async def test_prompt_carries_history_and_prev(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=60))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()  # 09:00-10:00 改海报
        service._clock.advance(minutes=61)
        provider.reply = seg_reply(event="回消息和对进度", minutes=45)
        await service.ensure_fresh()
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("已经过完的时段", prompt)
        self.assertIn("09:00-10:00 改海报", prompt)
        self.assertIn("已经 61 分钟", prompt)

    async def test_sleep_crosses_midnight_via_carry(self):
        """23:00 决定睡到 07:00：今天截到 24:00，明天 00:00 起由结转接上，零点不问模型。"""
        evening = datetime(2026, 8, 22, 23, 0, tzinfo=TZ)
        provider = FakeProvider("p", reply=seg_reply(event="睡觉", minutes=480))
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [provider], moment=evening)
        await service.ensure_fresh()
        segs = service.segments()
        self.assertEqual(segs[-1]["end"], "24:00")
        self.assertEqual(store.get("segment_carry", {}).get("end"), "07:00")
        # 睡眠窗口要按完整的一觉算：23:00→07:00
        window = sleep_window_minutes(service.current_slots())
        self.assertEqual(window, (23 * 60, 7 * 60))

        # 跨过午夜
        service._clock.advance(minutes=90)  # 次日 00:30
        self.assertEqual(service._clock.today_str(), "2026-08-23")
        segs = service.segments()
        self.assertEqual(len(segs), 1, "结转接成今天的第一段")
        self.assertEqual(segs[0]["start"], "00:00")
        self.assertEqual(segs[0]["end"], "07:00")
        self.assertEqual(service.active_segment()["event"], "睡觉")
        self.assertFalse(service.refresh_due(), "还在睡：不生成")
        self.assertEqual(provider.calls, 1, "零点不需要再问一次模型")
        self.assertEqual(service.status()["wake_at"], "07:00")

    async def test_failure_keeps_current_segment_and_backs_off(self):
        provider = FakeProvider("p", reply=seg_reply(minutes=30))
        clock = FakeClock()
        service, _, _ = self.build(
            cfg(
                schedule_provider_name="p",
                schedule_provider_cooldown_minutes=30,
                schedule_retry_interval_seconds=0,
            ),
            [provider],
            monotonic=clock,
        )
        await service.ensure_fresh()
        self.assertEqual(service.source, SOURCE_LLM)

        provider.error = RuntimeError("down")
        service._clock.advance(minutes=31)  # 段过完
        self.assertTrue(service.request_refresh())
        await service._task
        self.assertGreater(service.retry_after, 0, "失败后应进入退避窗口")
        self.assertTrue(service.segments(), "模型挂了也要按身体现算一段，不留白")
        self.assertEqual(service.source, SOURCE_TEMPLATE)

        calls = provider.calls
        for _ in range(10):
            if service.request_refresh():  # 本地段占着位时仍会投递，但退避期内不砸模型
                await service._task
        self.assertEqual(provider.calls, calls, "退避窗口内不再砸模型")

        clock.advance(30 * 60 + 1)
        provider.error = None
        provider.reply = seg_reply(event="回过神来干活", minutes=60)
        self.assertTrue(service.request_refresh())
        await service._task
        self.assertEqual(service.source, SOURCE_LLM, "退避一过就让模型接手")
        self.assertGreater(provider.calls, calls)

    async def test_llm_failure_with_active_segment_keeps_it(self):
        """模型失败但手上这段还没做完：接着做手上的事，不换也不留白。"""
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=120))
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_retry_interval_seconds=0), [provider]
        )
        await service.ensure_fresh()
        provider.error = RuntimeError("down")
        service._clock.advance(minutes=15)
        changed = await service.ensure_fresh(force=True)
        self.assertTrue(changed, "force 失败时按身体现算一段也算决定了")
        segs = service.segments()
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["event"], "改海报")
        self.assertEqual(segs[0]["end"], "09:15", "旧段被截到新段起点")

    async def test_single_flight_under_concurrency(self):
        provider = FakeProvider("p", reply=seg_reply(), delay=0.05)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        results = await asyncio.gather(*(service.ensure_fresh() for _ in range(10)))
        self.assertEqual(provider.calls, 1, "10 个并发请求只应产生 1 次模型调用")
        self.assertEqual(sum(1 for r in results if r), 1)

    async def test_unparsable_reply_backs_off(self):
        junk = FakeProvider("p", reply="我今天很忙，没法决定哦～")
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_retry_interval_seconds=0), [junk]
        )
        # 解析失败也要按身体现算一段（不留白），同时进退避
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertTrue(service.segments(), "解析失败不留白：本地兜底排一段")
        self.assertIn("无法解析", service.last_error)
        self.assertGreater(service.retry_after, 0, "解析失败同样进退避，否则每 30 秒砸一次模型")

    async def test_use_llm_schedule_off_never_calls_model(self):
        provider = FakeProvider("p", reply=seg_reply())
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", use_llm_schedule=False), [provider]
        )
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertTrue(service.segments(), "本地也要排得出一段")
        service._clock.advance(minutes=120)
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 0, "无模型模式永远不砸模型")

    async def test_seed_first_segment(self):
        service, _, _ = self.build()
        self.assertTrue(service.seed_first_segment())
        self.assertFalse(service.seed_first_segment(), "已有段就不重种")
        segs = service.segments()
        self.assertEqual(len(segs), 1)
        self.assertTrue(segs[0]["event"])

    async def test_legacy_full_day_schedule_is_truncated_to_now(self):
        """旧版整表日程升级：还没到的时段读的时候丢掉，未来不预存。"""
        service, store, _ = self.build()
        store.set("today_date", FIXED_TODAY)
        store.set(
            "daily_schedule",
            [
                {"start": "00:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.15},
                {"start": "08:00", "end": "09:00", "event": "洗漱早餐", "energy_rate": 0.05},
                {"start": "09:00", "end": "12:00", "event": "上午工作", "energy_rate": -0.1},
                {"start": "12:00", "end": "18:00", "event": "下午安排", "energy_rate": -0.08},
                {"start": "18:00", "end": "24:00", "event": "休闲", "energy_rate": 0.02},
            ],
        )
        store.set("schedule_source", SOURCE_LLM)
        segs = service.segments()
        self.assertEqual([s["event"] for s in segs], ["睡眠", "洗漱早餐", "上午工作"])
        self.assertEqual(service.active_segment()["event"], "上午工作")
        self.assertFalse(service.refresh_due(), "当前段还在：升级当天无缝接上")

    async def test_new_day_without_carry_starts_fresh(self):
        provider = FakeProvider("p", reply=seg_reply(event="昨天的最后一段", minutes=60))
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        service._clock.advance(minutes=16 * 60)  # 次日 01:00
        self.assertEqual(service.segments(), [], "跨天没有结转：昨天那段不算今天的")
        self.assertTrue(service.refresh_due())
        provider.reply = seg_reply(event="刚起没多久，弄点吃的", minutes=40)
        self.assertTrue(await service.ensure_fresh())
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("昨天的收尾", prompt, "新一天的第一段要能看到昨天的收尾")

    async def test_request_refresh_spawns_once(self):
        provider = FakeProvider("p", reply=seg_reply(), delay=0.05)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        self.assertTrue(service.request_refresh())
        self.assertFalse(service.request_refresh(), "已有任务在跑时不应叠加")
        await asyncio.sleep(0.2)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(service.source, SOURCE_LLM)
        await service.aclose()

    async def test_aclose_cancels_inflight(self):
        provider = FakeProvider("p", reply=seg_reply(), delay=5.0)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        service.request_refresh()
        await asyncio.sleep(0.01)
        await service.aclose()
        self.assertFalse(service.generating)

    async def test_status_report(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=60))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        status = service.status()
        self.assertEqual(status["source"], SOURCE_LLM)
        self.assertEqual(status["date"], today())
        self.assertEqual(status["slots"], 1)
        self.assertIn("改海报", status["active"])
        self.assertEqual(status["activity"]["name"], "改海报")
        self.assertEqual(status["generated_at"], "2026-08-22 09:00:00")
        self.assertEqual(status["last_error"], "")
        self.assertFalse(status["generating"], "空闲时不该报告正在生成")
        self.assertFalse(status["pending_today"])

    async def test_generating_flag_tracks_actual_call(self):
        provider = FakeProvider("p", reply=seg_reply(), delay=0.1)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        self.assertFalse(service.generating)
        task = asyncio.create_task(service.ensure_fresh())
        await asyncio.sleep(0.03)
        self.assertTrue(service.generating, "模型调用飞行中应为 True")
        self.assertTrue(service.status()["generating"])
        await task
        self.assertFalse(service.generating)
        self.assertFalse(service.status()["generating"])

    async def test_generating_resets_after_failure(self):
        provider = FakeProvider("p", error=RuntimeError("down"))
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        self.assertFalse(service.generating, "失败后也必须复位")

    async def test_force_bypasses_backoff(self):
        clock = FakeClock()
        broken = FakeProvider("p", error=RuntimeError("down"))
        conf = cfg(schedule_provider_name="p", schedule_retry_interval_seconds=0)
        service, _, _ = self.build(conf, [broken], monotonic=clock)
        await service.ensure_fresh()
        before = broken.calls
        await service.ensure_fresh(force=True, ignore_cooldown=True)
        self.assertGreater(broken.calls, before, "/重置日程 应能绕过退避窗口")

    async def test_success_clears_backoff(self):
        provider = FakeProvider("p", error=RuntimeError("down"))
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_retry_interval_seconds=0), [provider]
        )
        await service.ensure_fresh()
        self.assertGreater(service.retry_after, 0)
        provider.error = None
        provider.reply = seg_reply()
        await service.ensure_fresh(force=True, ignore_cooldown=True)
        self.assertEqual(service.retry_after, 0)


class ProcessFollowsSegmentTest(unittest.IsolatedAsyncioTestCase):
    """过程跟着段走：同一件事续段不重开，换了事现开新过程。"""

    def build(self):
        conf = cfg()
        self.conf = conf
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(FIXED_TODAY, conf.cycle_length)
        clock = FrozenClock(FIXED_MOMENT)
        schedule = ScheduleService(store.scope, lambda: conf, clock)
        process = ProcessService(store.scope, lambda: conf, clock, schedule)
        return schedule, process, store

    async def test_same_event_keeps_process(self):
        schedule, process, store = self.build()
        schedule.seed_first_segment()
        first = process.current()
        first["slot_event"] = "改海报"
        first["slot_start"] = "09:00"
        first["expected_end"] = (FIXED_MOMENT + __import__("datetime").timedelta(minutes=120)).isoformat()
        store.set("current_process", first)

        schedule._clock.advance(minutes=30)
        process.note_segment_changed({"event": "改海报", "start": "09:30", "end": "11:00"})
        after = process.current()
        self.assertEqual(after["slot_event"], "改海报")
        self.assertEqual(after["slot_start"], "09:30", "锚点跟到新的一段")
        self.assertEqual(after["phase"], first["phase"], "阶段不重开")

    async def test_different_event_reopens_process(self):
        schedule, process, store = self.build()
        schedule.seed_first_segment()
        first = process.current()
        first["name"] = "改海报"
        first["slot_event"] = "改海报"
        first["slot_start"] = "09:00"
        store.set("current_process", first)

        schedule.on_install = lambda previous, current: process.note_segment_changed(current)
        schedule._clock.advance(minutes=30)
        schedule._append_segment(
            {"start": "09:30", "end": "10:30", "event": "下楼取快递", "location": "驿站",
             "emotion": "随性", "energy_rate": -0.03},
            FIXED_TODAY,
        )
        after = process.current()
        self.assertEqual(after["slot_event"], "下楼取快递")
        self.assertEqual(after["name"], "下楼取快递")
        self.assertIn("改海报", process.recent() or [], "旧过程收进历史")


if __name__ == "__main__":
    unittest.main()
