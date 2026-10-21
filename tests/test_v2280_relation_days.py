"""v2.28.0：把「认识了几天」加进提示词（C3，用户明确要求的有意反转）。

旧规则是「认识多久不进上下文」，理由是模型会拿去念「我们认识这么久了」。用户现在
明确要求加进去，所以注入里给一句陈述事实的短句；关系深浅仍由位置词承载，面板仍给
准数与「个月/年」档。`test_v222_injection.test_days_reach_the_context` 是那条旧测试
按新语义改的，这里补的是边界：刚认识、老关系、只出现一次、不出现档位话。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.prompt_builder import PromptBuilder
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 9, 26, 13, 3, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


def make_core() -> HumanoidCoreInstance:
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
    store.load(TODAY, 28)
    config = cfg()
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: config,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=FakeContext(), gateway=None,
    )
    clock = FrozenClock(MOMENT)
    core.clock = clock
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        service = getattr(core, name, None)
        if service is not None and hasattr(service, "_clock"):
            service._clock = clock
    return core


class RelationDaysTest(unittest.TestCase):
    def test_long_relationship_reports_days(self):
        core = make_core()
        core.mood.profile("42")["first_met"] = core.now_epoch() - 400 * 86400
        text = core.build_injection("42", is_group=False)
        self.assertIn("你们已经认识 400 天了", text)
        self.assertEqual(text.count("你们已经认识"), 1, f"认识天数说了不止一次：{text}")

    def test_fresh_relationship_says_just_met(self):
        core = make_core()
        core.mood.profile("42")["first_met"] = core.now_epoch() - 3600
        text = core.build_injection("42", is_group=False)
        self.assertIn("你们刚认识不久", text)
        self.assertNotIn("你们已经认识", text)

    def test_exactly_one_day_counts(self):
        core = make_core()
        core.mood.profile("42")["first_met"] = core.now_epoch() - 86400
        text = core.build_injection("42", is_group=False)
        self.assertIn("你们已经认识 1 天了", text)

    def test_old_panel_phrases_stay_out_of_the_injection(self):
        core = make_core()
        core.mood.profile("42")["first_met"] = core.now_epoch() - 400 * 86400
        text = core.build_injection("42", is_group=False)
        for banned in ("认识几天了", "个月了", "认识 1 年"):
            self.assertNotIn(banned, text, f"面板档位话漏进上下文：{banned}\n{text}")

    def test_panel_still_has_the_precise_value(self):
        core = make_core()
        core.mood.profile("42")["first_met"] = core.now_epoch() - 400 * 86400
        panel = "\n".join(core.status_lines("42"))
        self.assertIn("认识 1 年", panel)
        self.assertEqual(PromptBuilder._days_line(3 * 86400), "认识 3 天")

    def test_sleeping_also_carries_relation_and_days(self):
        """睡着时的必需品里也要有认识天数（与关系/称呼同级，不是生活细节）。"""
        core = make_core()
        core.schedule._scope.update_self(
            today_date=TODAY,
            daily_schedule=[{"start": "00:00", "end": "24:00", "event": "睡眠",
                             "location": "卧室", "emotion": "平静", "energy_rate": 0.1}],
        )
        core.soma.data["asleep_since"] = core.soma.now - 3600.0
        core.mood.profile("42")["first_met"] = core.now_epoch() - 10 * 86400
        text = core.build_injection("42", is_group=False)
        self.assertIn("你们已经认识 10 天了", text)
        self.assertIn("正睡着", text)


if __name__ == "__main__":
    unittest.main()
