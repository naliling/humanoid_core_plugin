"""日程：读路径不阻塞、生成单飞、时段规范化。"""

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
from humanoid.services.schedule import SOURCE_LLM, SOURCE_TEMPLATE, ScheduleService, build_prompt
from humanoid.slots import (
    DAY_MINUTES,
    coverage_is_complete,
    find_slot,
    format_time,
    normalize_slots,
    parse_time,
)
from humanoid.state import StateStore

from .fakes import FakeClock, FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def today() -> str:
    """与状态文件同一天：调度服务靠日期比较判断跨天，测试里两边必须一致。"""
    return FIXED_TODAY


GOOD_SCHEDULE = json.dumps(
    [
        {"start": "00:00", "end": "07:30", "event": "睡眠", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},
        {"start": "07:30", "end": "09:00", "event": "早餐", "location": "餐厅", "emotion": "清醒", "energy_rate": 0.05},
        {"start": "09:00", "end": "12:00", "event": "工作", "location": "书房", "emotion": "专注", "energy_rate": -0.1},
        {"start": "12:00", "end": "18:00", "event": "下午安排", "location": "书房", "emotion": "认真", "energy_rate": -0.08},
        {"start": "18:00", "end": "24:00", "event": "休闲与入睡", "location": "客厅", "emotion": "轻松", "energy_rate": 0.02},
    ],
    ensure_ascii=False,
)


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


class PromptTest(unittest.TestCase):
    def test_prompt_limits_slot_count(self):
        prompt = build_prompt(cfg(schedule_max_slots=16), "2026-08-22", "六")
        self.assertIn("最多 16 个时段", prompt)
        self.assertIn("对齐到 15 分钟", prompt)
        self.assertIn("00:00 开始", prompt)
        # 绝不能回到「逐格切出时段样例」那种写法
        self.assertNotIn("00:15-00:30", prompt)

    def test_default_prompt_fills_all_96_cells(self):
        """默认 96 格：模型可以逐格填，也可以合并，但 24 小时每一格都得有安排。"""
        prompt = build_prompt(cfg(), "2026-08-22", "六")
        self.assertIn("96 格", prompt)
        self.assertIn("最多 96 个时段", prompt)
        self.assertIn("把 24 小时每一格都填满", prompt)

    def test_flexible_granularity_drops_alignment(self):
        prompt = build_prompt(cfg(schedule_time_granularity="flexible"), "2026-08-22", "六")
        self.assertIn("不必对齐", prompt)

    def test_hourly_granularity(self):
        prompt = build_prompt(cfg(schedule_time_granularity="hourly"), "2026-08-22", "六")
        self.assertIn("对齐到 60 分钟", prompt)

    def test_empty_extra_is_omitted(self):
        prompt = build_prompt(cfg(schedule_prompt_extra=""), "2026-08-22", "六")
        self.assertNotIn("可选偏好", prompt)

    def test_extra_is_a_reference_not_an_order(self):
        """补充偏好要明说「仅供参考」，不能写成必须执行的指令。"""
        prompt = build_prompt(cfg(schedule_prompt_extra="偏爱户外"), "2026-08-22", "六")
        self.assertIn("偏爱户外", prompt)
        self.assertIn("仅供参考", prompt)
        self.assertNotIn("必须", prompt.split("【可选偏好】")[1].split("【表格格式】")[0])

    def test_body_values_are_references(self):
        """身体数值进 prompt 时必须标明是参考，且不带「所以该安排什么」的结论。"""
        body = {
            "energy": 42.0,
            "hunger": 77.0,
            "sleep_pressure": 81.0,
            "sleep_debt": 3.5,
            "last_sleep_hours": 5.5,
            "cycle": "处于【经期】，身体能量消耗较大",
        }
        prompt = build_prompt(cfg(), "2026-08-22", "六", body=body, now_text="14:30")
        self.assertIn("【她此刻的身体参考数值】", prompt)
        self.assertIn("是参考，不是必须满足的条件", prompt)
        self.assertIn("精力：42", prompt)
        self.assertIn("饥饿：77", prompt)
        self.assertIn("睡眠债：3.5 小时", prompt)
        self.assertIn("当前时间：14:30", prompt)
        for conclusion in ("必须安排", "应该早点睡", "需要补觉安排", "务必"):
            self.assertNotIn(conclusion, prompt)

    def test_no_body_values_still_works(self):
        prompt = build_prompt(cfg(), "2026-08-22", "六")
        self.assertNotIn("【她此刻的身体参考数值】", prompt)
        self.assertIn("【表格格式】", prompt)

    def test_prompt_carries_no_content_hints(self):
        """v2.16.7 起不再替模型思考：内容提示一律不进 prompt。"""
        prompt = build_prompt(cfg(), "2026-08-22", "六")
        for hint in (
            "要有真的会打断聊天的事",
            "不要排成每分钟都在做有意义的事",
            "「改第三季度的报表」比「上班」有用",
        ):
            self.assertNotIn(hint, prompt)


class ScheduleServiceTest(unittest.IsolatedAsyncioTestCase):
    def build(self, config=None, providers=None, global_provider=None, monotonic=None):
        conf = config or cfg()
        self.conf = conf
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load("2026-08-22", conf.cycle_length)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=list(providers or []), global_provider=global_provider)
        resolver = ProviderResolver(ctx, log)
        # 同一个可注入计时源交给 gateway 与 service：否则一方用真实单调时钟、一方用仿真
        # 时钟，「退避到期后应能重试」这类断言会在两层冷却之间错乱。
        clock_source = monotonic or time.monotonic
        gateway = LLMGateway(resolver, lambda: self.conf, log, clock_source)
        clock = FrozenClock(FIXED_MOMENT)
        service = ScheduleService(
            store.scope, lambda: self.conf, clock, logger=log, monotonic=clock_source
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, store, log

    async def test_read_path_is_sync_and_never_touches_provider(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE, delay=10.0)
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        slots = service.current_slots()  # 同步调用，绝不 await
        self.assertTrue(coverage_is_complete(slots))
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertEqual(provider.calls, 0, "读路径不允许调用模型")
        self.assertIsNone(
            store.get("today_date"), "临时模板不得落盘，否则会把已生成的大模型日程覆盖掉"
        )

    async def test_current_slot_returns_something_usable(self):
        service, _, _ = self.build()
        slot = service.current_slot(minutes=13 * 60)
        self.assertIn("event", slot)
        self.assertTrue(slot["event"])

    async def test_generation_replaces_template(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        changed = await service.ensure_fresh()
        self.assertTrue(changed)
        self.assertEqual(service.source, SOURCE_LLM)
        self.assertEqual(store.get("schedule_source"), SOURCE_LLM)
        self.assertTrue(coverage_is_complete(service.current_slots()))
        self.assertEqual(service.current_slots()[0]["event"], "睡眠")

    async def test_single_flight_under_concurrency(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE, delay=0.05)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        results = await asyncio.gather(*(service.ensure_fresh() for _ in range(10)))
        self.assertEqual(provider.calls, 1, "10 个并发请求只应产生 1 次模型调用")
        self.assertEqual(sum(1 for r in results if r), 1)

    async def test_second_call_same_day_is_noop(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 1)

    async def test_refresh_interval_regenerates(self):
        """动态日程：到了重排间隔就重排，没到就不动。"""
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", schedule_refresh_minutes=15), [provider]
        )
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 1)
        self.assertFalse(service.refresh_due(), "同一时刻不该判为到期待重排")

        await service.ensure_fresh()
        self.assertEqual(provider.calls, 1, "没到间隔不应重排")

        service._clock.advance(minutes=15)
        self.assertTrue(service.refresh_due(), "过了一个重排间隔就该判到期")
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 2, "到期后应重新生成")

    async def test_due_refresh_respects_backoff(self):
        """到点重排不能绕过失败退避：模型挂掉时 due 恒为真，不挡会退回每 30 秒一次的永久重试。"""
        clock = FakeClock()
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(
            cfg(
                schedule_provider_name="p",
                schedule_refresh_minutes=15,
                schedule_provider_cooldown_minutes=30,
                schedule_retry_interval_seconds=0,
            ),
            [provider],
            monotonic=clock,
        )
        await service.ensure_fresh()
        self.assertEqual(service.source, SOURCE_LLM)

        provider.error = RuntimeError("down")
        service._clock.advance(minutes=15)
        self.assertTrue(service.refresh_due(), "过了一个重排间隔就该判到期")
        self.assertTrue(service.request_refresh(), "还没有退避时，到点重排应该尝试")
        await service._task
        self.assertGreater(service.retry_after, 0, "失败后应进入退避窗口")
        calls = provider.calls

        for _ in range(10):
            self.assertFalse(
                service.request_refresh(),
                "退避窗口内，哪怕 due 一直为真也不该再投递",
            )
        self.assertEqual(provider.calls, calls, "退避窗口内不应再调用模型")

        # 退避到期后，到点重排恢复尝试；用户强制（/重置日程）则随时可硬闯。
        clock.advance(30 * 60 + 1)
        self.assertEqual(service.retry_after, 0)
        self.assertTrue(service.request_refresh())
        await service._task
        self.assertGreater(provider.calls, calls)

    async def test_body_values_travel_into_the_prompt(self):
        """重排时身体参考数值要进 prompt，且每次现取活值。"""
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        readings = {"energy": 55.0, "hunger": 60.0, "sleep_pressure": 70.0, "sleep_debt": 2.0}
        conf = cfg(schedule_provider_name="p", schedule_refresh_minutes=15)
        self.conf = conf
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load("2026-08-22", conf.cycle_length)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=[provider])
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: self.conf, log)
        clock = FrozenClock(FIXED_MOMENT)
        service = ScheduleService(
            store.scope, lambda: self.conf, clock, logger=log, body_provider=lambda: dict(readings)
        )
        service.set_resolver_gateway(resolver, gateway)
        await service.ensure_fresh()
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("【她此刻的身体参考数值】", prompt)
        self.assertIn("精力：55", prompt)
        self.assertIn("饥饿：60", prompt)
        self.assertIn("是参考，不是必须满足的条件", prompt)

        readings.update({"energy": 20.0, "hunger": 90.0})
        clock.advance(minutes=15)
        await service.ensure_fresh()
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("精力：20", prompt, "重排时该拿到新的活值")
        self.assertIn("饥饿：90", prompt)

    async def test_force_regenerates(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        await service.ensure_fresh(force=True)
        self.assertEqual(provider.calls, 2)

    async def test_failure_keeps_template_and_records_error(self):
        broken = FakeProvider("p", error=RuntimeError("connection refused"))
        service, store, _ = self.build(cfg(schedule_provider_name="p"), [broken])
        changed = await service.ensure_fresh()
        self.assertFalse(changed)
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertTrue(coverage_is_complete(service.current_slots()))
        self.assertIn("connection refused", service.last_error)
        self.assertFalse(
            store.get("daily_schedule"), "失败时只能拿临时模板展示，不能把模板写进持久状态"
        )

    async def test_timeout_keeps_template(self):
        # 直接让 provider 抛 TimeoutError，等价于 wait_for 触发超时，但不用真等
        slow = FakeProvider("p", error=asyncio.TimeoutError())
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [slow])
        changed = await service.ensure_fresh()
        self.assertFalse(changed)
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertIn("超时", service.last_error)
        self.assertTrue(coverage_is_complete(service.current_slots()))

    async def test_unparsable_reply_keeps_template(self):
        junk = FakeProvider("p", reply="我今天很忙，没法给你日程哦～")
        service, _, log = self.build(cfg(schedule_provider_name="p"), [junk])
        self.assertFalse(await service.ensure_fresh())
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertIn("无法解析", service.last_error)

    async def test_garbage_slots_are_rejected(self):
        junk = FakeProvider("p", reply='[{"start": "x", "end": "y"}, "nope"]')
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [junk])
        self.assertFalse(await service.ensure_fresh())
        self.assertEqual(service.source, SOURCE_TEMPLATE)

    async def test_partial_slots_are_repaired_not_rejected(self):
        partial = FakeProvider(
            "p",
            reply='[{"start":"09:00","end":"12:00","event":"工作","energy_rate":-0.1}]',
        )
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [partial])
        self.assertTrue(await service.ensure_fresh())
        slots = service.current_slots()
        self.assertTrue(coverage_is_complete(slots))
        self.assertEqual(service.source, SOURCE_LLM)

    async def test_slot_count_capped_from_model_output(self):
        dense = json.dumps(
            [
                {
                    "start": format_time(i * 15),
                    "end": format_time((i + 1) * 15),
                    "event": f"块{i}",
                    "energy_rate": 0.0,
                }
                for i in range(96)
            ],
            ensure_ascii=False,
        )
        provider = FakeProvider("p", reply=dense)
        service, _, _ = self.build(cfg(schedule_provider_name="p", schedule_max_slots=16), [provider])
        self.assertTrue(await service.ensure_fresh())
        self.assertLessEqual(len(service.current_slots()), 16)

    async def test_use_llm_schedule_off_never_calls_model(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(
            cfg(schedule_provider_name="p", use_llm_schedule=False), [provider]
        )
        self.assertFalse(await service.ensure_fresh())
        self.assertFalse(service.request_refresh())
        self.assertEqual(provider.calls, 0)
        self.assertTrue(coverage_is_complete(service.current_slots()))

    async def test_global_fallback_used_when_ids_missing(self):
        glob = FakeProvider("global_one", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(
            cfg(schedule_provider_name="typo", schedule_allow_global_fallback=True),
            [],
            global_provider=glob,
        )
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(service.source, SOURCE_LLM)
        self.assertEqual(glob.calls, 1)

    async def test_strict_mode_falls_back_to_template(self):
        service, _, _ = self.build(
            cfg(schedule_provider_name="typo", schedule_allow_global_fallback=False),
            [],
            global_provider=FakeProvider("global_one", reply=GOOD_SCHEDULE),
        )
        self.assertFalse(await service.ensure_fresh())
        self.assertEqual(service.source, SOURCE_TEMPLATE)

    async def test_request_refresh_spawns_once(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE, delay=0.05)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        self.assertTrue(service.request_refresh())
        self.assertFalse(service.request_refresh(), "已有任务在跑时不应叠加")
        await asyncio.sleep(0.2)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(service.source, SOURCE_LLM)
        await service.aclose()

    async def test_aclose_cancels_inflight(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE, delay=5.0)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        service.request_refresh()
        await asyncio.sleep(0.01)
        await service.aclose()
        self.assertFalse(service.generating)

    async def test_status_report(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        service, _, _ = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        status = service.status()
        self.assertEqual(status["source"], SOURCE_LLM)
        self.assertEqual(status["date"], today())
        self.assertGreater(status["slots"], 0)
        self.assertEqual(status["generated_at"], "2026-08-22 09:00:00")
        self.assertEqual(status["last_error"], "")
        self.assertFalse(status["generating"], "空闲时不该报告正在生成")
        self.assertFalse(status["pending_today"])

    async def test_generating_flag_tracks_actual_call(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE, delay=0.1)
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

    async def test_failure_backoff_prevents_per_message_storm(self):
        """模型持续不可用时，不该每条消息都投递一次后台生成。"""
        clock = FakeClock()
        broken = FakeProvider("p", error=RuntimeError("down"))
        conf = cfg(
            schedule_provider_name="p",
            schedule_provider_cooldown_minutes=30,
            schedule_retry_interval_seconds=0,
        )
        service, store, log = self.build(conf, [broken], monotonic=clock)

        await service.ensure_fresh()
        calls_after_first = broken.calls
        self.assertGreater(calls_after_first, 0)
        self.assertGreater(service.retry_after, 0)

        # 后续 20 次「消息触发」都应被退避窗口挡住
        for _ in range(20):
            self.assertFalse(service.request_refresh())
            await service.ensure_fresh()
        self.assertEqual(broken.calls, calls_after_first, "退避窗口内不应再调用模型")

        # 退避到期后可以再试
        clock.advance(30 * 60 + 1)
        self.assertEqual(service.retry_after, 0)
        await service.ensure_fresh()
        self.assertGreater(broken.calls, calls_after_first)

    async def test_force_bypasses_backoff(self):
        clock = FakeClock()
        broken = FakeProvider("p", error=RuntimeError("down"))
        conf = cfg(schedule_provider_name="p", schedule_retry_interval_seconds=0)
        service, store, log = self.build(conf, [broken], monotonic=clock)
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
        provider.reply = GOOD_SCHEDULE
        await service.ensure_fresh(force=True, ignore_cooldown=True)
        self.assertEqual(service.retry_after, 0)




if __name__ == "__main__":
    unittest.main()
