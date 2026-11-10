"""v3.1.17：补掉 v3.1.16 剩下的四个缺口。

审计（v3.1.16 之后）暴露的问题，都不是「模型不听话」，是**判据和入口本身有洞**：

1. 编造漏判：`invented_facts` 拿「开会」去匹配「开了个会」，子串对不上，
   于是「去公司开了个会」在没有人设依据时也照样进日程；
2. 低信息量放行：「忙自己的事」语法通顺、不碰设备，闸门查不到，排进日程等于没排；
3. 质量计数只在内存：重启归零，也没有「这句被拒过」的记忆，同一句每轮重新发明；
4. 身体 9 个活值只有 6 个进 prompt：冷/不适/亲近/想说话在排日程时看不见。

本文件逐条钉住修法，并验证「被拒的写法不再回到候选清单」。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.core_instance import HumanoidCoreInstance
from humanoid.llm import CallGate, LLMGateway, ProviderResolver
from humanoid.services import schedule as schedule_mod
from humanoid.services.schedule import (
    QUALITY_STATS_KEY,
    REJECTS_KEY,
    SOURCE_TEMPLATE,
    ScheduleService,
    body_reference_block,
    invented_facts,
)
from humanoid.services.schedule_content import (
    build_slate,
    content_issues,
    fix_hint,
    low_info_issues,
)

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg, seg_reply

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)

SOMA_SNAPSHOT = {
    "sleep_pressure": 40.0,
    "sleep_debt": 1.5,
    "hunger": 55.0,
    "discomfort": 12.0,
    "chill": 8.0,
    "arousal": 35.0,
    "social_desire": 62.0,
    "asleep": False,
    "last_sleep_hours": 7.0,
}


class LowInfoTest(unittest.TestCase):
    """「忙自己的事」这种句子要判出来，但不能混进「设备/含糊」硬约束里。"""

    def test_placeholder_work_is_low_info(self):
        self.assertEqual(low_info_issues("忙自己的事"), ["含糊:忙自己的事"])
        self.assertEqual(low_info_issues("手头的活做完了"), ["含糊:手头的活"])
        self.assertEqual(low_info_issues("找点事做"), ["含糊:找点事做"])

    def test_other_placeholder_work_is_low_info(self):
        """v3.1.24：模型会把处境词原样抄进日程，这些说法同样不算一件事。"""
        self.assertEqual(low_info_issues("手头有东西要处理"), ["含糊:手头有东西要处理"])
        self.assertEqual(low_info_issues("手上有东西在忙"), ["含糊:手上有东西"])
        self.assertEqual(low_info_issues("有点事要处理"), ["含糊:有点事要处理"])
        self.assertEqual(low_info_issues("什么也不干"), ["含糊:什么也不干"])
        self.assertEqual(low_info_issues("坐着发愣"), ["含糊:坐着发愣"])

    def test_status_segments_are_displayable_but_plain_low_info_is_not(self):
        """v3.1.29：空话一律不显示，带 status 也一样；真处境词照常显示。"""
        from humanoid.services.schedule_content import clean_slots, is_displayable

        self.assertFalse(is_displayable({"event": "忙手里的活", "status": True}))
        self.assertFalse(is_displayable({"event": "忙手里的活"}))
        self.assertFalse(is_displayable({"event": "什么也不干，坐着发了会儿呆"}))
        self.assertTrue(is_displayable({"event": "把碗洗了，灶台擦了一遍"}))
        self.assertTrue(is_displayable({"event": "歇着", "status": True}))
        self.assertTrue(is_displayable({"event": "睡一觉", "status": True}))
        kept = clean_slots(
            [
                {"event": "把碗洗了"},
                {"event": "什么也不干"},
                {"event": "忙手里的活", "status": True},
                {"event": "赖床", "status": True},
            ]
        )
        self.assertEqual([item["event"] for item in kept], ["把碗洗了", "赖床"])

    def test_concrete_events_are_not_low_info(self):
        self.assertEqual(low_info_issues("把碗洗了，灶台擦了一遍"), [])
        self.assertEqual(low_info_issues("翻了两页书就放下了"), [])
        self.assertEqual(low_info_issues(""), [])

    def test_low_info_is_not_a_hard_content_issue(self):
        """硬约束（设备/含糊词）与低信息量分开：前者任何出口都不许有。"""
        self.assertEqual(content_issues("忙自己的事"), [])

    def test_fix_hint_speaks_about_actions_for_low_info(self):
        hint = fix_hint(["含糊:忙自己的事"])
        self.assertIn("处境", hint)
        self.assertIn("具体动作", hint)
        self.assertNotIn("她的世界没有手机", hint)

    def test_fix_hint_still_covers_device_words(self):
        self.assertIn("她的世界没有手机", fix_hint(["设备:手机"]))


class FactCheckNormalizationTest(unittest.TestCase):
    """口语变形不该把编造漏过去；同时不许把「煮了碗面」判成编造。"""

    def test_past_tense_infix_is_normalized(self):
        self.assertEqual(invented_facts("去公司开了个会", "她喜欢画画"), ["开会"])
        self.assertEqual(invented_facts("上了一天班", "她喜欢画画"), ["上班"])
        self.assertEqual(invented_facts("跟同事打了个招呼", "她喜欢画画"), ["同事"])

    def test_evidence_still_clears_it(self):
        self.assertEqual(invented_facts("去公司开了个会", "她的工作是在公司上班"), [])
        self.assertEqual(invented_facts("给猫添了粮", "她养了猫"), [])

    def test_everyday_actions_are_not_invented(self):
        self.assertEqual(invented_facts("给自己煮了碗面", "她喜欢画画"), [])
        self.assertEqual(invented_facts("把被子抱出去晒", "她喜欢画画"), [])


class BodyReferenceBlockTest(unittest.TestCase):
    """soma 的 9 个活值都要进 prompt，少一个模型就只能猜。"""

    def test_every_soma_value_is_rendered(self):
        body = dict(SOMA_SNAPSHOT)
        body.update({"energy": 66.0, "cycle": "第 3 天"})
        block = body_reference_block(body, "08:07")
        for label in ("精力", "饥饿", "睡眠压力", "睡眠债", "昨晚睡了",
                      "身体不适", "觉得冷", "亲近", "想跟人说话", "醒着", "生理周期"):
            self.assertIn(label, block, f"身体参考里少了「{label}」")
        self.assertIn("会随时间真实变化", block)

    def test_asleep_flag_is_rendered_as_state(self):
        body = dict(SOMA_SNAPSHOT)
        body["asleep"] = True
        self.assertIn("睡着的", body_reference_block(body, "03:00"))

    def test_partial_body_only_renders_what_it_has(self):
        block = body_reference_block({"energy": 50.0}, "08:07")
        self.assertIn("精力", block)
        self.assertNotIn("饥饿", block)


class ScheduleBodyWiringTest(unittest.TestCase):
    """`_schedule_body` 真的把 soma 的 9 个键递出去了（不是只在 prompt 层支持）。"""

    def _core(self):
        core = object.__new__(HumanoidCoreInstance)
        core.clock = FrozenClock(FIXED_MOMENT)
        conf = type("C", (), {"soma_enabled": True})()
        core._config_provider = lambda: conf

        class _Energy:
            energy = 71.5

            @staticmethod
            def cycle_description():
                return "第 3 天"

        class _Soma:
            @staticmethod
            def snapshot():
                return dict(SOMA_SNAPSHOT)

        core.energy = _Energy()
        core.soma = _Soma()
        return core

    def test_all_live_values_reach_the_schedule(self):
        body = self._core()._schedule_body()
        for key in SOMA_SNAPSHOT:
            self.assertIn(key, body, f"_schedule_body 少了 {key}")
        self.assertEqual(body["energy"], 71.5)
        self.assertEqual(body["cycle"], "第 3 天")
        self.assertEqual(body["now"], "09:00")


class RejectBlacklistTest(unittest.TestCase):
    """被拒的写法不再摆回候选清单（否则同一句每轮重新发明）。"""

    def test_rejected_events_are_filtered_from_slate(self):
        items = build_slate(
            [{"event": "把碗洗了", "location": "厨房", "minutes": 20}],
            rejected=["把碗洗了"],
        )
        self.assertEqual(items, [])

    def test_unrelated_rejects_do_not_block_everything(self):
        items = build_slate(
            [{"event": "把碗洗了", "location": "厨房", "minutes": 20}],
            rejected=["忙自己的事"],
        )
        self.assertEqual([item["event"] for item in items], ["把碗洗了"])


class _SlateRecorder(FakeProvider):
    """记录每次生成 prompt，回固定文本。"""

    def __init__(self, provider_id: str, reply: str) -> None:
        super().__init__(provider_id, reply=reply)
        self.prompts: list[str] = []

    async def text_chat(self, prompt: str | None = None, **kwargs):
        if prompt and "【审查这一段】" not in prompt:
            self.prompts.append(str(prompt))
        return await super().text_chat(prompt=prompt, **kwargs)


class _ServiceCase(unittest.IsolatedAsyncioTestCase):
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
        return service, store.scope


class LowInfoGateTest(_ServiceCase):
    """模型写「忙自己的事」：重写一次还这样，就不许它进日程。"""

    async def test_low_info_never_lands(self):
        provider = _SlateRecorder("p", seg_reply(event="忙自己的事", minutes=60))
        service, scope = self.build(provider)
        await service.ensure_fresh()

        segs = service.segments()
        self.assertEqual(len(segs), 1)
        event = str(segs[0]["event"])
        self.assertEqual(low_info_issues(event), [], f"低信息量事件落库了：{event}")
        self.assertEqual(content_issues(event), [])
        # 降级优先给具体候选，实在没有才退回处境词。
        self.assertTrue(event.strip())
        self.assertGreaterEqual(service._quality["low_info"], 1)
        self.assertGreaterEqual(service._quality["fallback"], 1)

    async def test_counts_are_persisted_and_reject_is_remembered(self):
        provider = _SlateRecorder("p", seg_reply(event="忙自己的事", minutes=60))
        service, scope = self.build(provider)
        await service.ensure_fresh()

        stats = scope.get_self(QUALITY_STATS_KEY, {}) or {}
        self.assertGreaterEqual(int(stats.get("low_info", 0)), 1, "质量计数没有落盘")
        self.assertGreaterEqual(int(stats.get("fallback", 0)), 1)
        rejects = scope.get_self(REJECTS_KEY, []) or []
        events = [str(item.get("event") or "") for item in rejects]
        self.assertIn("忙自己的事", events, "被拒的写法没有记住")

    async def test_rejected_wording_does_not_come_back_in_the_next_slate(self):
        provider = _SlateRecorder("p", seg_reply(event="忙自己的事", minutes=60))
        service, scope = self.build(provider)
        scope.set_self(REJECTS_KEY, [{"event": "把碗洗了", "reason": "content"}])
        await service.ensure_fresh()
        prompt = provider.prompts[0]
        self.assertNotIn("把碗洗了", prompt, "被拒的写法又摆回候选清单了")

    async def test_quality_gate_off_still_blocks_low_info(self):
        provider = _SlateRecorder("p", seg_reply(event="忙自己的事", minutes=60))
        service, _scope = self.build(provider, )
        await service.ensure_fresh()
        event = str(service.segments()[0]["event"])
        self.assertEqual(low_info_issues(event), [], f"关掉质量闸后低信息量落库了：{event}")

    async def test_status_flagged_low_info_does_not_sneak_through(self):
        """v3.1.25：模型回 `status: true` 时也要验低信息量。

        线上真实形态：模型直接回 `{"status": true, "event": "手头有东西要处理"}`，
        旧逻辑因为带 status 就跳过了低信息量判定，日程里于是多出一段空话。
        """
        provider = _SlateRecorder(
            "p", seg_reply(event="手头有东西要处理", minutes=60, status=True)
        )
        service, _scope = self.build(provider)
        await service.ensure_fresh()
        segs = service.segments()
        self.assertEqual(len(segs), 1)
        event = str(segs[0]["event"])
        self.assertNotEqual(event, "手头有东西要处理", "带 status 的空话落库了")
        self.assertEqual(low_info_issues(event), [], f"仍然是空话：{event}")
        self.assertEqual(content_issues(event), [])

    async def test_clean_event_is_left_alone(self):
        provider = _SlateRecorder("p", seg_reply(event="把碗洗了，灶台擦了一遍", minutes=45))
        service, _scope = self.build(provider)
        await service.ensure_fresh()
        segs = service.segments()
        self.assertEqual(segs[0]["event"], "把碗洗了，灶台擦了一遍")
        self.assertEqual(service.source, "llm")


class StatsSurfaceTest(_ServiceCase):
    async def test_stats_expose_total_and_rejects(self):
        provider = _SlateRecorder("p", seg_reply(event="忙自己的事", minutes=60))
        service, _scope = self.build(provider)
        await service.ensure_fresh()
        stats = service.status()
        self.assertIn("quality_total", stats)
        self.assertIn("rejects", stats)
        self.assertGreaterEqual(int(stats["quality_total"].get("low_info", 0)), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
