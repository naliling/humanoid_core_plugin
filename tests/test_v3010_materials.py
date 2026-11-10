"""v3.0.10：日程生成的素材池 + 「睡够了别再睡」。

两个老毛病同源——生成时手里没有足够的东西可用：
* 模型只能在「睡、吃、收拾、看手机」之间换词，一天高度雷同；
* 中午在模型眼里是默认睡觉点，一天能睡出两三段。

素材池只扩大「可能做什么」，不替她决定；睡够了还睡则在生成后独立判一遍。
用例只调公开/半公开入口，不复制实现里的字面量。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.services.life_material import (
    DEFAULT_COUNT,
    MATERIALS,
    materials_for,
    render_materials,
    slot_name,
)
from humanoid.services.schedule import (
    NAP_REDUNDANT_MINUTES,
    ScheduleService,
    segment_prompt,
)

from humanoid.llm import LLMGateway, ProviderResolver

from .fakes import (
    FakeContext,
    FakeProvider,
    FrozenClock,
    RecordingLogger,
    ScopeStore,
)
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 13, 0, tzinfo=TZ)
PERSONA = "我是个上班族，家里养了一只橘猫，喜欢看书和画画。"
SEED = ["role-1", "2026-08-22"]


def _items(minute=13 * 60, persona=PERSONA, recent=(), count=DEFAULT_COUNT, seed=SEED):
    return materials_for(seed, persona, minute, recent, count)


class SlotNameTest(unittest.TestCase):
    def test_slots_cover_the_clock(self):
        self.assertEqual(slot_name(2 * 60), "凌晨")
        self.assertEqual(slot_name(7 * 60), "早上")
        self.assertEqual(slot_name(10 * 60), "上午")
        self.assertEqual(slot_name(12 * 60), "中午")
        self.assertEqual(slot_name(15 * 60), "下午")
        self.assertEqual(slot_name(19 * 60), "傍晚")
        self.assertEqual(slot_name(23 * 60), "夜里")


class MaterialsPoolTest(unittest.TestCase):
    def test_pool_never_contains_sleep(self):
        """素材里不许出现睡觉：睡不睡由身体读数决定，不是从清单里挑。"""
        for item in MATERIALS:
            self.assertNotIn("睡", str(item["event"]), str(item["event"]))

    def test_same_day_is_stable(self):
        first = [str(it["event"]) for it in _items()]
        second = [str(it["event"]) for it in _items()]
        self.assertEqual(first, second)

    def test_different_day_differs(self):
        a = [str(it["event"]) for it in _items(seed=["role-1", "2026-08-22"])]
        b = [str(it["event"]) for it in _items(seed=["role-1", "2026-09-30"])]
        self.assertNotEqual(a, b)

    def test_batch_is_mixed_not_one_band(self):
        bands = {str(it["band"]) for it in _items()}
        self.assertGreaterEqual(len(bands), 3, f"批次太单薄：{bands}")

    def test_count_is_respected(self):
        self.assertEqual(len(_items(count=6)), 6)
        self.assertEqual(len(_items(count=DEFAULT_COUNT)), DEFAULT_COUNT)

    def test_slot_filter_uses_this_hour(self):
        noon = _items(minute=12 * 60)
        night = _items(minute=2 * 60)
        self.assertTrue(noon and night)
        for item in noon:
            self.assertIn("中午", item["slots"])
        for item in night:
            self.assertIn("凌晨", item["slots"])

    def test_pet_material_needs_a_pet(self):
        """「只适用于人设确认有宠物」：没人设时这条不该出现。"""
        without = {str(it["event"]) for it in _items(persona="", count=40)}
        with_pet = {str(it["event"]) for it in _items(count=40)}
        pet_items = [it for it in MATERIALS if "pet" in (it.get("needs") or ())]
        self.assertTrue(pet_items, "池子里应该至少有一条需要宠物的素材")
        for item in pet_items:
            self.assertNotIn(str(item["event"]), without)
        self.assertTrue(any(str(it["event"]) in with_pet for it in pet_items))

    def test_job_material_needs_a_job(self):
        without = {str(it["event"]) for it in _items(persona="")}
        duty_items = [it for it in MATERIALS if "job" in (it.get("needs") or ())]
        self.assertTrue(duty_items)
        for item in duty_items:
            self.assertNotIn(str(item["event"]), without)

    def test_recent_topic_is_deduped(self):
        """今天已经做过的事不再发——否则素材本身就成了雷同的来源。"""
        first = _items()
        target = first[0]
        again = _items(recent=[str(target["topic"])])
        self.assertNotIn(str(target["event"]), {str(it["event"]) for it in again})

    def test_dedupe_does_not_hollow_the_batch(self):
        recent = [str(it["topic"]) for it in _items()]
        self.assertEqual(len(_items(recent=recent)), DEFAULT_COUNT)


class RenderMaterialsTest(unittest.TestCase):
    def test_rows_carry_event_location_and_minutes(self):
        text = render_materials(_items(count=3), SEED)
        rows = [row for row in text.splitlines() if row.strip()]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(row.startswith("  - "), row)
            self.assertIn("约 ", row)
            self.assertIn("分钟", row)

    def test_minutes_stay_inside_the_range(self):
        for item in _items():
            text = render_materials([item], SEED)
            lo, hi = item["minutes"]
            minutes = int(text.split("约 ")[1].split(" 分钟")[0])
            self.assertGreaterEqual(minutes, lo)
            self.assertLessEqual(minutes, hi)


class PromptBlockTest(unittest.TestCase):
    def _prompt(self, **kwargs):
        body = kwargs.pop("body", {})
        return segment_prompt(
            cfg(),
            now_text="12:30",
            weekday=3,
            persona=None,
            body=body,
            prev=None,
            elapsed_minutes=0,
            history=[],
            history_note="",
            ongoing=[],
            **kwargs,
        )

    def test_materials_block_appears(self):
        text = render_materials(_items(count=3), SEED)
        prompt = self._prompt(materials=text)
        self.assertIn("【今天她可能做的事】", prompt)
        self.assertIn("不是清单", prompt)
        for row in text.splitlines():
            self.assertIn(row.strip(), prompt)

    def test_no_materials_no_block(self):
        self.assertNotIn("【今天她可能做的事】", self._prompt())

    def test_sleep_block_reports_today(self):
        prompt = self._prompt(day_sleep_minutes=95)
        self.assertIn("【今天睡了多少】", prompt)
        self.assertIn("95 分钟", prompt)

    def test_no_sleep_no_block(self):
        self.assertNotIn("【今天睡了多少】", self._prompt())

    def test_nap_rule_when_slept_enough(self):
        prompt = self._prompt(day_sleep_minutes=NAP_REDUNDANT_MINUTES)
        self.assertIn("这一段别再排睡", prompt)

    def test_nap_rule_not_before_threshold(self):
        prompt = self._prompt(day_sleep_minutes=NAP_REDUNDANT_MINUTES - 1)
        self.assertNotIn("这一段别再排睡", prompt)

    def test_nap_rule_yields_to_sleep_pressure(self):
        """真困的时候让路：困意越线那条规则仍然放行补觉。"""
        prompt = self._prompt(
            day_sleep_minutes=NAP_REDUNDANT_MINUTES + 30,
            body={"sleep_pressure": 88.0},
        )
        self.assertNotIn("这一段别再排睡", prompt)
        self.assertIn("困意已经 80+/100", prompt)


def _service(moment=MOMENT, sleep_pressure=40.0, **overrides):
    conf = cfg(**overrides)
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = ScopeStore(tmp)
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    return ScheduleService(
        store.scope,
        lambda: conf,
        FrozenClock(moment),
        logger=RecordingLogger(),
        monotonic=lambda: 10_000.0,
        body_provider=lambda: {"sleep_pressure": sleep_pressure},
    )


def _seed_slots(service, slots):
    service._scope.update_self(
        today_date=service._clock.today_str(),
        daily_schedule=list(slots),
    )


def _nap(start="10:00", end="11:30"):
    return {
        "start": start,
        "end": end,
        "event": "睡了个午觉",
        "location": "卧室",
        "emotion": "困",
        "energy_rate": 0.0,
    }


def _awake(start="11:30", end="12:40"):
    return {
        "start": start,
        "end": end,
        "event": "在厨房弄点吃的",
        "location": "厨房",
        "emotion": "平静",
        "energy_rate": -0.02,
    }


class SleepTodayTest(unittest.TestCase):
    def test_counts_only_passed_sleep(self):
        service = _service()
        _seed_slots(service, [_nap(), _awake()])
        self.assertEqual(service._sleep_today(13 * 60), 90)

    def test_future_part_is_not_counted(self):
        service = _service()
        _seed_slots(service, [_nap("10:00", "15:00")])
        self.assertEqual(service._sleep_today(13 * 60), 180)

    def test_awake_segments_do_not_count(self):
        service = _service()
        _seed_slots(service, [_awake("09:00", "12:00")])
        self.assertEqual(service._sleep_today(13 * 60), 0)


class NeedlessSleepTest(unittest.TestCase):
    def test_slept_enough_in_daytime_is_needless(self):
        service = _service()
        _seed_slots(service, [_nap("09:00", "11:00")])
        self.assertTrue(service._needless_sleep(_nap("13:00", "14:00"), 13 * 60))

    def test_short_nap_is_not_needless(self):
        service = _service()
        _seed_slots(service, [_nap("09:00", "10:00")])
        self.assertFalse(service._needless_sleep(_nap("13:00", "14:00"), 13 * 60))

    def test_tired_still_allowed_to_sleep(self):
        service = _service(sleep_pressure=90.0)
        _seed_slots(service, [_nap("09:00", "11:00")])
        self.assertFalse(service._needless_sleep(_nap("13:00", "14:00"), 13 * 60))

    def test_night_sleep_is_never_needless(self):
        night = datetime(2026, 8, 22, 23, 30, tzinfo=TZ)
        service = _service(moment=night)
        # 夜里这一觉从 22:00 起：日程推出来的夜窗罩住此刻，白天那套判据不适用。
        _seed_slots(service, [_nap("22:00", "24:00")])
        self.assertFalse(service._needless_sleep(_nap("23:30", "24:00"), 23 * 60 + 30))

    def test_awake_slot_is_not_needless(self):
        service = _service()
        _seed_slots(service, [_nap("09:00", "11:00")])
        self.assertFalse(service._needless_sleep(_awake("13:00", "14:00"), 13 * 60))


def _sleep_reply(minutes=60):
    return (
        '{"continue": false, "event": "睡了个午觉", "location": "卧室",'
        ' "emotion": "困", "energy_rate": 0.0, "minutes": ' + str(minutes) + "}"
    )


class OverlongNapTest(unittest.TestCase):
    def _day(self, sleep_pressure=40.0):
        """上午已经睡过一觉的下午：今天不是头一觉，判据才该生效。"""
        service = _service(sleep_pressure=sleep_pressure)
        _seed_slots(service, [_nap("00:00", "07:00")])
        return service

    def test_five_hour_nap_in_daytime_is_overlong(self):
        self.assertTrue(self._day()._overlong_nap(_nap("13:00", "18:00")))

    def test_three_hour_nap_is_fine(self):
        self.assertFalse(self._day()._overlong_nap(_nap("13:00", "16:00")))

    def test_tired_can_sleep_long(self):
        self.assertFalse(self._day(92.0)._overlong_nap(_nap("13:00", "18:00")))

    def test_first_sleep_of_the_day_is_never_flagged(self):
        """她今天还没睡过：这一觉可能就是夜里那觉拖到现在，不能判太长。"""
        service = _service()
        self.assertFalse(service._overlong_nap(_nap("09:00", "17:00")))

    def test_night_sleep_is_not_a_nap(self):
        night = datetime(2026, 8, 22, 23, 30, tzinfo=TZ)
        service = _service(moment=night)
        _seed_slots(service, [_nap("00:00", "07:00"), _nap("22:00", "24:00")])
        self.assertFalse(service._overlong_nap(_nap("22:00", "24:00")))

    def test_awake_slot_is_not_a_nap(self):
        self.assertFalse(self._day()._overlong_nap(_awake("13:00", "18:00")))


class NeedLessSleepGateTest(unittest.IsolatedAsyncioTestCase):
    """生成后独立判一遍：重写一次仍然排睡，就放行但记一笔。"""

    def _build(self, reply, moment=MOMENT, sleep_pressure=40.0):
        conf = cfg(
            schedule_provider_name="p",
            schedule_change_chance=0,
        )
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=[FakeProvider("p", reply=reply)])
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: conf, log, lambda: 10_000.0)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment),
            logger=log,
            monotonic=lambda: 10_000.0,
            body_provider=lambda: {"sleep_pressure": sleep_pressure},
        )
        service.set_resolver_gateway(resolver, gateway)
        return service

    async def test_needless_sleep_is_counted_and_let_through(self):
        service = self._build(_sleep_reply())
        _seed_slots(service, [_nap("09:00", "11:00")])
        await service.ensure_fresh()
        self.assertGreaterEqual(
            service._quality.get("needless_sleep", 0), 1,
            "今天已经睡够、困意也没越线，重写后仍排睡：应该记一笔",
        )
        self.assertEqual(len(service.segments()), 2, "记一笔但放行，不降级")

    async def test_overlong_nap_is_counted(self):
        service = self._build(_sleep_reply(300))
        _seed_slots(service, [_nap("00:00", "07:00")])
        await service.ensure_fresh()
        self.assertGreaterEqual(
            service._quality.get("overlong_nap", 0), 1,
            "白天睡五小时、困意也没越线：应该记一笔",
        )

    async def test_tired_day_is_not_flagged(self):
        service = self._build(_sleep_reply(), sleep_pressure=92.0)
        _seed_slots(service, [_nap("09:00", "11:00")])
        await service.ensure_fresh()
        self.assertEqual(service._quality.get("needless_sleep", 0), 0)


class QualityFixBlockTest(unittest.TestCase):
    def test_needless_sleep_hint_is_in_the_rewrite_prompt(self):
        from humanoid.services.schedule import _quality_fix_block

        block = _quality_fix_block(["needless_sleep"], [])
        self.assertIn("别再排睡", block)

    def test_overlong_hint_is_in_the_rewrite_prompt(self):
        from humanoid.services.schedule import _quality_fix_block

        block = _quality_fix_block(["overlong_nap"], [])
        self.assertIn("太长", block)


if __name__ == "__main__":
    unittest.main()
