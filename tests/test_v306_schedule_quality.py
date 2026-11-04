"""v3.0.6 日程增强的回归用例：3 分钟也是一段、范例带生活条件、当天去重、事实来源闸。

这四条对应同一个老毛病的不同侧面：**prompt 里的规则是请求，不是保证**。

* 15 分钟下限不只是一行常量：prompt 写着「最短 15 分钟」、`_segment_minutes` 抬到 15、
  起点又按 15 分钟刻度向下对齐——三处叠加，把「倒杯水」变成半小时。现在短段不再
  被推到刻度上（否则 10:07 做的 5 分钟事会被记成 10:00 开始）。
* 范例是最容易被照着抄的东西：一条「给猫添粮」递给没有猫的角色，就是在替她编一只猫。
  生活条件只留在代码侧（`needs`），不递进 prompt。
* 今天上午做过的事下午又排一遍，看起来不是她过日子，是模型在填表。
* 事件里凭空出现「女儿」「客户」「预约」，合法 JSON 只说明格式对——这里判有没有依据。

用例只调公开/半公开入口，不复制实现里的字面量。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import LLMResult, OUTCOME_OK
from humanoid.persona import Persona
from humanoid.services.schedule import (
    EXAMPLE_BANDS,
    EXAMPLE_POOL,
    SEGMENT_MIN_MINUTES,
    ScheduleService,
    example_capabilities,
    examples_for,
    invented_facts,
    parse_segment,
    segment_prompt,
    _render_examples,
)

from .fakes import FakeContext, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg, seg_reply

TZ = ZoneInfo("Asia/Shanghai")


def _service(moment, persona_text=None, **overrides):
    conf = cfg(**overrides)
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    persona = (
        (lambda: Persona(name="她", prompt=persona_text, source="test"))
        if persona_text is not None
        else None
    )
    service = ScheduleService(
        store.scope,
        lambda: conf,
        FrozenClock(moment),
        logger=RecordingLogger(),
        persona_provider=persona,
        monotonic=lambda: 10_000.0,
    )
    return service, conf


class ScriptedGateway:
    """按顺序吐出预设文本的替身；调用次数与最后一次 prompt 都留得下来。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts: list[str] = []

    async def generate(self, **kwargs):
        self.prompts.append(str(kwargs.get("prompt") or ""))
        text = self.texts[min(len(self.prompts) - 1, len(self.texts) - 1)]
        return LLMResult(ok=True, text=text, outcome=OUTCOME_OK, label="fake", provider_id="p1")


class ShortSegmentTest(unittest.TestCase):
    """P0：分钟级的动作就是几分钟，而且不许被推到刻度上。"""

    def test_floor_is_three_minutes(self):
        self.assertEqual(SEGMENT_MIN_MINUTES, 3)

    def test_five_minutes_stays_five(self):
        slot = parse_segment({"event": "倒杯水", "minutes": 5}, now_minute=9 * 60, step=15)
        self.assertEqual((slot["start"], slot["end"]), ("09:00", "09:05"), "5 分钟的事被抬成了别的时长")

    def test_below_the_floor_climbs_to_the_floor(self):
        slot = parse_segment({"event": "洗把脸", "minutes": 1}, now_minute=9 * 60, step=15)
        self.assertEqual(slot["end"], "09:03", f"下限应是 {SEGMENT_MIN_MINUTES} 分钟")

    def test_a_short_segment_starts_now_not_on_the_grid(self):
        """10:07 做的 5 分钟事，起点该是 10:07；对齐到 10:00 等于把已经过完的时间又排一遍。"""
        slot = parse_segment({"event": "回条消息", "minutes": 5}, now_minute=10 * 60 + 7, step=15)
        self.assertEqual(slot["start"], "10:07")

    def test_a_long_segment_still_snaps_to_the_grid(self):
        slot = parse_segment({"event": "写方案", "minutes": 90}, now_minute=10 * 60 + 20, step=15)
        self.assertEqual(slot["start"], "10:15")


class ExamplePoolTest(unittest.TestCase):
    """P1：池子够宽，但生活条件只在人设确实有时才发。"""

    def test_pool_has_thirty_items(self):
        self.assertEqual(len(EXAMPLE_POOL), 30)
        self.assertEqual(sum(len(band) for band in EXAMPLE_BANDS), 30)

    def test_every_band_has_two_general_items(self):
        for index, band in enumerate(EXAMPLE_BANDS):
            general = [item for item in band if not item.get("needs")]
            self.assertGreaterEqual(len(general), 2, f"第 {index} 档通用范例不够，没有宠物的人会缺档")

    def test_capabilities_come_from_the_persona_text(self):
        self.assertIn("job", example_capabilities("她在一家书店上班"))
        self.assertIn("pet", example_capabilities("她养了一只橘猫"))
        self.assertEqual(example_capabilities("她喜欢在阳台晒太阳"), set())

    def test_examples_never_leak_the_condition_into_the_prompt(self):
        rendered = _render_examples(examples_for(["r1", "2026-08-22"]))
        self.assertNotIn("needs", rendered)

    def test_a_persona_without_a_cat_never_gets_the_cat_example(self):
        for seed in (["r1", "2026-08-22"], ["r1", "2026-08-23"], ["r2", "2026-08-24"]):
            picked = examples_for(seed, "她喜欢在阳台晒太阳")
            self.assertFalse(
                any("猫" in str(item.get("event")) for item in picked),
                f"没有猫的人设拿到了猫的范例：{picked}",
            )

    def test_a_persona_with_a_job_can_get_the_work_example(self):
        caps = example_capabilities("她是一名小学老师")
        self.assertIn("job", caps)


class FactSourceTest(unittest.TestCase):
    """P3：凭空出现的身份/家人/宠物/约定要能判出来。"""

    def test_a_child_appears_out_of_nowhere(self):
        self.assertEqual(invented_facts("下午去接孩子放学", "她是一名插画师"), ["孩子"])

    def test_a_child_that_the_persona_mentions_is_fine(self):
        self.assertEqual(invented_facts("下午去接孩子放学", "她有一个女儿，上小学"), [])

    def test_a_pet_needs_a_pet_in_the_persona(self):
        self.assertEqual(invented_facts("给猫添了点粮", "她是一名插画师"), ["猫"])
        self.assertEqual(invented_facts("给猫添了点粮", "她养了一只猫"), [])

    def test_no_evidence_means_no_verdict(self):
        """读不到人设 ≠ 人设里没有这只猫：没有判定基准就不判，宁可放过。"""
        self.assertEqual(invented_facts("下午去接孩子放学", ""), [])

    def test_ordinary_events_are_not_flagged(self):
        for event in ("去楼下便利店买点东西", "把碗洗了", "躺着刷手机", "睡了一觉"):
            self.assertEqual(invented_facts(event, "她是一名插画师"), [], f"误判：{event}")


class PromptRulesTest(unittest.TestCase):
    """prompt 侧的两条新规则：短段与当天去重。"""

    def _prompt(self):
        service, conf = _service(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        return segment_prompt(
            conf,
            now_text="09:00",
            weekday="周六",
            persona=None,
            body={},
            prev=None,
            elapsed_minutes=0,
            history=[],
            history_note="",
            ongoing=[],
            is_night=False,
        )

    def test_minutes_rule_says_three_minutes_is_a_segment(self):
        text = self._prompt()
        self.assertIn(f"{SEGMENT_MIN_MINUTES} 分钟也是一段", text)
        self.assertIn("上下限，不是目标值", text)

    def test_dedupe_rule_is_in_the_prompt(self):
        text = self._prompt()
        self.assertIn("别原样再排一遍", text)
        self.assertIn("同一类事一天最多两段", text)


class QualityGateTest(unittest.TestCase):
    """P2/P3：判中了先重写一次，仍然编造就降级成中性段。"""

    def _run(self, service, gateway):
        service.set_resolver_gateway(FakeContext(), gateway)
        asyncio.run(service.ensure_fresh(force=True))
        return service.status()

    def test_invented_fact_survives_the_rewrite_then_degrades(self):
        service, _ = _service(
            datetime(2026, 8, 22, 9, 0, tzinfo=TZ), persona_text="她是一名插画师"
        )
        gateway = ScriptedGateway(
            [
                seg_reply(event="下午去接孩子放学"),
                seg_reply(event="去接孩子放学，顺便买了菜"),
            ]
        )
        status = self._run(service, gateway)
        self.assertEqual(len(gateway.prompts), 2, "判中了却没有重写一次")
        self.assertIn("上一版没通过", gateway.prompts[1])
        active = str(status.get("active") or "")
        self.assertNotIn("孩子", active, "人设里没有孩子，日程里却留下了接孩子")
        self.assertEqual(status["source"], "template", "仍然编造时该降级成中性处境词")
        self.assertEqual(status["quality"]["rewrite"], 1)
        self.assertEqual(status["quality"]["invented"], 1)
        self.assertEqual(status["quality"]["fallback"], 1)

    def test_a_good_rewrite_is_used(self):
        service, _ = _service(
            datetime(2026, 8, 22, 9, 0, tzinfo=TZ), persona_text="她是一名插画师"
        )
        gateway = ScriptedGateway(
            [
                seg_reply(event="下午去接孩子放学"),
                seg_reply(event="在书桌前画一张稿子"),
            ]
        )
        status = self._run(service, gateway)
        self.assertIn("画", str(status.get("active") or ""))
        self.assertEqual(status["source"], "llm")
        self.assertEqual(status["quality"]["rewrite"], 1)
        self.assertEqual(status["quality"]["invented"], 0)

    def test_today_already_had_it_twice(self):
        service, _ = _service(datetime(2026, 8, 22, 15, 0, tzinfo=TZ))
        service._scope.update_self(
            today_date="2026-08-22",
            daily_schedule=[
                {"start": "07:00", "end": "07:40", "event": "在厨房做早饭", "location": "厨房"},
                {"start": "12:00", "end": "12:40", "event": "在厨房做早饭", "location": "厨房"},
            ]
        )
        gateway = ScriptedGateway(
            [seg_reply(event="在厨房做早饭"), seg_reply(event="在厨房做早饭")]
        )
        status = self._run(service, gateway)
        self.assertEqual(status["quality"]["repeat"], 1, "第三次排同一件事没被记下来")
        self.assertEqual(len(gateway.prompts), 2)

    def test_continuing_the_previous_segment_is_not_a_repeat(self):
        service, _ = _service(datetime(2026, 8, 22, 10, 0, tzinfo=TZ))
        service._scope.update_self(
            today_date="2026-08-22",
            daily_schedule=[
                {"start": "09:00", "end": "10:00", "event": "在书桌前画稿子", "location": "书房"},
            ]
        )
        gateway = ScriptedGateway([seg_reply(event="在书桌前画稿子", minutes=40)])
        status = self._run(service, gateway)
        self.assertEqual(len(gateway.prompts), 1, "接着做同一件事被当成了重复")
        self.assertEqual(status["quality"]["repeat"], 0)

    def test_the_gate_can_be_turned_off(self):
        service, _ = _service(
            datetime(2026, 8, 22, 9, 0, tzinfo=TZ),
            persona_text="她是一名插画师",
            schedule_quality_gate=False,
        )
        gateway = ScriptedGateway([seg_reply(event="下午去接孩子放学")])
        status = self._run(service, gateway)
        self.assertEqual(len(gateway.prompts), 1)
        self.assertEqual(status["quality"]["rewrite"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
