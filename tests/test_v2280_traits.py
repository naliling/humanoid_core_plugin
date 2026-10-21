"""v2.28.0：人设特质识别（C1/C5）。

用户原话：「不是每一个角色都是勤劳的；所有功能每个生产模块都应该有识别机制与更多
参考细节」。这一层把「夜猫子 / 早起 / 勤快 / 懒散 / 宅 / 外向 / 倒班 / 学生 / 上班」
从人设正文里读出来，供日程 prompt 与过程服务使用。

测试重点不是「能识别多少词」，而是：
* 只认明确正向词，简单否定不算（「不熬夜」「不爱出门」）；
* 互相矛盾的线索（勤快 × 懒散）只输出一条，不让 prompt 自己打架；
* 拿不到特质时，过程模板选择与旧版逐字段一致（这条最容易在重构里悄悄变）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from humanoid import traits
from humanoid.clock import Clock
from humanoid.config import HumanoidConfig
from humanoid.role_scope import RoleScope
from humanoid.services import process as process_module
from humanoid.services.process import (
    _LOW_INTENSITY_STYLES,
    _RELAXED_STYLES,
    PROCESS_TEMPLATES,
    ProcessService,
)


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


class _FakeSchedule:
    def __init__(self, event: str = "工作", start: str = "09:00", end: str = "18:00"):
        self._slot = {"event": event, "start": start, "end": end}

    def current_slot(self, minutes=None):
        return dict(self._slot)


class DetectTest(unittest.TestCase):
    def test_night_owl_words(self):
        for text in ("她是夜猫子", "经常熬夜", "白天补觉，晚上才精神", "上夜班", "倒班", "通宵写东西"):
            with self.subTest(text=text):
                self.assertTrue(traits.detect(text).night_owl, text)

    def test_early_bird_words(self):
        for text in ("她习惯早起", "早睡早起", "清晨去晨跑", "作息规律"):
            with self.subTest(text=text):
                self.assertTrue(traits.detect(text).early_bird, text)

    def test_diligent_words(self):
        for text in ("她很勤快", "自律的人", "工作狂", "闲不住"):
            with self.subTest(text=text):
                self.assertTrue(traits.detect(text).diligent, text)

    def test_lazy_words(self):
        for text in ("她有点懒散", "爱摸鱼", "习惯拖延", "喜欢躺平", "性子随性"):
            with self.subTest(text=text):
                self.assertTrue(traits.detect(text).lazy, text)

    def test_homebody_and_outgoing(self):
        home = traits.detect("她是个宅女，社恐，不爱出门")
        self.assertTrue(home.homebody)
        self.assertFalse(home.outgoing, "「不爱出门」不该反过来命中外向")
        outgoing = traits.detect("她很外向，爱出门，朋友多")
        self.assertTrue(outgoing.outgoing)
        self.assertFalse(outgoing.homebody)

    def test_identity_words(self):
        got = traits.detect("她还是学生，每天上课；周末去打工，平时要上班")
        self.assertTrue(got.student)
        self.assertTrue(got.worker)
        shift = traits.detect("她在医院上夜班，三班倒")
        self.assertTrue(shift.shift_worker)

    def test_empty_and_garbage_inputs_are_safe(self):
        for bad in (None, "", "   ", 0, [], {"a": 1}):
            with self.subTest(bad=bad):
                got = traits.detect(bad)
                self.assertIsInstance(got, traits.PersonaTraits)
                self.assertFalse(got.night_owl)
                self.assertEqual(got.hints, ())

    def test_simple_negation_does_not_hit(self):
        self.assertFalse(traits.detect("她不熬夜").night_owl)
        self.assertFalse(traits.detect("她不是夜猫子，从不晚睡").night_owl)
        self.assertFalse(traits.detect("她不算勤快").diligent)
        self.assertFalse(traits.detect("她不爱出门").outgoing, "「爱出门」被「不爱」否掉了")
        # 「不爱出门」本身是宅的词表项，要命中。
        self.assertTrue(traits.detect("她不爱出门").homebody)

    def test_negation_does_not_cross_clause_boundary(self):
        """上一句的「不」不该否掉下一句的「熬夜」。"""
        got = traits.detect("她不爱早起，晚上经常熬夜")
        self.assertTrue(got.night_owl)
        self.assertFalse(got.early_bird)

    def test_positive_occurrence_after_negated_one_still_hits(self):
        """人设里先写「不熬夜」再写「最近总熬夜」：不能只看第一处。"""
        self.assertTrue(traits.detect("以前不熬夜，最近总熬夜").night_owl)

    def test_conflicting_hints_keep_only_one(self):
        got = traits.detect("她工作狂，但也常常懒散拖延")
        hints = " ".join(got.hints)
        self.assertIn("不算勤快", hints)
        self.assertNotIn("勤快自律", hints, f"勤快与懒散同时进了提示词：{got.hints}")
        # bool 本身可以都保留（下游按「更明确」的那个走），矛盾只在提示词层解决。
        self.assertTrue(got.lazy and got.diligent)

    def test_trait_hints_night_owl_mentions_midnight(self):
        got = traits.detect("她是夜猫子，凌晨才睡")
        joined = " ".join(got.hints)
        self.assertIn("夜猫子", joined)
        self.assertIn("00:00-06:00", joined, f"没把「凌晨也是完整一天」说清楚：{got.hints}")

    def test_trait_hints_accepts_none(self):
        self.assertEqual(traits.trait_hints(None), ())
        self.assertEqual(traits.trait_hints(SimpleNamespace()), ())

    def test_neutral_persona_has_no_hints(self):
        got = traits.detect("她喜欢花，喜欢安静地听歌。")
        self.assertEqual(got.hints, ())


class ProcessTemplateTraitTest(unittest.TestCase):
    """C5：过程服务读人设特质选模板，但**不改变类别推断**、不罢工。"""

    def _service(self, traits_provider=None, event: str = "工作"):
        conf = cfg()
        scope = RoleScope({"_state_version": 3, "roles": {}}, "bot1", lambda: None)
        clock = Clock(lambda: conf)
        return ProcessService(
            scope, lambda: conf, clock, _FakeSchedule(event=event),
            traits_provider=traits_provider,
        )

    def _options(self, service, event: str) -> list:
        """拦下 random.choice，看它**拿到的模板池**（而不是抽中的那一个）。"""
        slot = {"event": event, "start": "09:00", "end": "18:00"}
        with mock.patch.object(
            process_module.random, "choice", side_effect=lambda seq: seq[0]
        ) as chooser:
            service._generate_process_for_slot(service._clock.now(), slot)
        return chooser.call_args[0][0]

    def test_no_traits_uses_the_old_pool(self):
        service = self._service()
        options = self._options(service, "工作")
        self.assertEqual(options, PROCESS_TEMPLATES["工作"], "没特质时不该过滤模板")
        # 空特质的默认路径也要与旧版一致。
        service = self._service(lambda: traits.PersonaTraits())
        self.assertEqual(self._options(service, "工作"), PROCESS_TEMPLATES["工作"])

    def test_lazy_prefers_low_intensity_for_work(self):
        lazy = traits.detect("她很懒散，爱拖延，常常躺平")
        service = self._service(lambda: lazy)
        options = self._options(service, "跟客户过方案")
        self.assertTrue(options)
        for item in options:
            self.assertIn(item["style"], _LOW_INTENSITY_STYLES, item)
        self.assertNotIn("专注", [item["style"] for item in options], "懒散角色做正事还排专注")

    def test_lazy_relaxed_for_free_time(self):
        lazy = traits.detect("她很懒散，爱拖延")
        service = self._service(lambda: lazy)
        options = self._options(service, "自由活动")
        self.assertTrue(options)
        for item in options:
            self.assertIn(item["style"], _RELAXED_STYLES, item)

    def test_diligent_keeps_the_existing_pool(self):
        diligent = traits.detect("她很自律，是个工作狂")
        service = self._service(lambda: diligent)
        self.assertEqual(self._options(service, "工作"), PROCESS_TEMPLATES["工作"])
        self.assertEqual(self._options(service, "复习功课"), PROCESS_TEMPLATES["学习"])

    def test_lazy_does_not_stop_working(self):
        """懒散不是罢工：这一段是日程定的，模板池可以偏向轻量，但不能为空。"""
        lazy = traits.detect("她懒散，爱摸鱼")
        service = self._service(lambda: lazy)
        proc = service._generate_process_for_slot(
            service._clock.now(), {"event": "跟客户过方案", "start": "09:00", "end": "18:00"}
        )
        self.assertTrue(proc.get("name"))
        self.assertEqual(proc["category"], "工作")

    def test_category_inference_is_unchanged(self):
        service = self._service()
        cases = {
            "跟客户过方案": "工作",
            "复习考试": "学习",
            "午餐与午休": "用餐",
            "睡眠": "睡眠",
            "自由活动": "自由时间",
            "出去跑步": "运动",
        }
        for event, expected in cases.items():
            with self.subTest(event=event):
                self.assertEqual(service._infer_category({"event": event}), expected)

    def test_traits_provider_failure_falls_back(self):
        def boom():
            raise RuntimeError("人设读不到")

        service = self._service(boom)
        self.assertEqual(self._options(service, "工作"), PROCESS_TEMPLATES["工作"])


if __name__ == "__main__":
    unittest.main()
