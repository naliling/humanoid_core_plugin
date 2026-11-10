"""v3.1.33：日程得按她的身体写（小马没有手、没有鞋），角色得有一份会变的基础信息。

两个真实来源：

* 用户原话「2 必须修 没按物种我的角色都是小马 哪来的鞋子 连人设都识别得不对」
  ——`webchat` 那天生成的是「把门口的鞋摆整齐，顺手擦了鞋柜」，而它的物种档案里
  写的是龙。物种识别本身早就有（`humanoid/species.py`），只是**日程链路从来没读过
  那份档案**：素材池按人写、候选不筛、提示词不说、硬闸也不认。
* 用户原话「这个插件 没有给每个角色可以拥有一套基础信息 如年龄 且也是会变得」
  ——之前年龄只活在人设正文里，只有 `ccb.py` 为判成年扫过一眼，日程拿不到；而且
  存一个「今年 23 岁」的字段必然过期。现在存登记年龄 + 登记日，读的时候现算。

测试分四层，从纯函数到服务：
    物种判定 → 素材池/候选过滤 → 提示词与硬闸 → 旧数据清理 → 基础信息算术。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid import role_basics
from humanoid.persona import Persona
from humanoid.services.life_material import materials_for
from humanoid.services.schedule import (
    PURGE_KEY,
    PURGE_VERSION,
    ScheduleService,
    build_slate,
    segment_prompt,
)
from humanoid.services.schedule_content import (
    clean_slots,
    is_displayable,
    species_issues,
    species_note,
)

from .fakes import FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
PONY = {"kind": "pony", "label": "天马", "traits": ["角", "翅膀", "四蹄"], "confidence": 0.92}
HUMAN = {"kind": "human", "label": "人类", "traits": [], "confidence": 0.9}


def slot(start: str, end: str, event: str, **extra) -> dict:
    body = {
        "start": start,
        "end": end,
        "event": event,
        "location": "家里",
        "energy_rate": 0.0,
    }
    body.update(extra)
    return body


class SpeciesIssuesTest(unittest.TestCase):
    """小马/兽类没有手指和鞋；人形一个词都不拦。"""

    def test_a_pony_cannot_wear_shoes(self):
        self.assertEqual(species_issues("把门口的鞋摆整齐，顺手擦了鞋柜", "pony"), ["物种:鞋柜"])

    def test_fingers_and_hair_are_human_only(self):
        self.assertIn("物种:手指", species_issues("手指上沾了点汁", "pony"))
        self.assertIn("物种:头发", species_issues("把头发吹干", "pony"))

    def test_a_human_or_unknown_kind_is_not_judged(self):
        for kind in ("human", "machine", ""):
            self.assertEqual(species_issues("把门口的鞋摆整齐，顺手擦了鞋柜", kind), [])

    def test_the_note_names_her_body(self):
        note = species_note("pony", "天马", ["角", "翅膀", "四蹄"])
        self.assertIn("天马", note)
        self.assertIn("蹄子", note)
        self.assertIn("没有鞋", note)
        self.assertEqual(species_note("human", "人类", []), "")


class MaterialFilterTest(unittest.TestCase):
    """素材池里那条「把门口的鞋摆整齐」必须按物种筛掉，人形时照旧能用。"""

    def _events(self, species: str) -> list[str]:
        items = materials_for([], "", 700, count=200, species=species)
        return [str(item.get("event") or "") for item in items]

    def test_the_shoe_item_is_filtered_for_a_pony(self):
        events = self._events("pony")
        self.assertTrue(events)  # 池子没被筛空
        for event in events:
            self.assertFalse(species_issues(event, "pony"), event)

    def test_a_human_still_gets_the_shoe_item(self):
        # 不保证一定抽得到，但池子里有它——直接查池子，别依赖抽样顺序。
        pool = materials_for([], "", 700, count=1000, species="")
        self.assertTrue(any("鞋" in str(item.get("event") or "") for item in pool))


class SlateFilterTest(unittest.TestCase):
    """选着写的时候，不是身体做得到的候选根本不该出现在清单里。"""

    def test_a_slate_for_a_pony_has_no_shoes(self):
        items = [
            {"event": "把门口的鞋摆整齐，顺手擦了鞋柜", "location": "门口", "minutes": 15},
            {"event": "把碗洗了", "location": "厨房", "minutes": 10},
        ]
        slate = build_slate(items, [], kind="pony")
        text = str(slate)
        self.assertIn("把碗洗了", text)
        self.assertNotIn("鞋", text)

    def test_without_a_kind_every_candidate_survives(self):
        items = [{"event": "把门口的鞋摆整齐", "location": "门口", "minutes": 15}]
        self.assertIn("鞋", str(build_slate(items, [])))


class DisplayAndPurgeTest(unittest.TestCase):
    """读的时候不显示，写的时候从 state 里删掉（purge v7），两处都得按物种。"""

    def build(self, species=PONY):
        conf = cfg(
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(datetime(2026, 8, 22, 13, 10, tzinfo=TZ)),
            logger=log,
            monotonic=lambda: 10_000.0,
        )
        if species:
            service._scope.update_self(today_date=FIXED_TODAY, species=dict(species))
        return service

    def test_a_shoe_segment_is_not_displayable_for_a_pony(self):
        shoe = slot("10:00", "10:30", "把门口的鞋摆整齐，顺手擦了鞋柜")
        self.assertFalse(is_displayable(shoe, kind="pony"))
        self.assertTrue(is_displayable(shoe))

    def test_clean_slots_drops_it(self):
        slots = [
            slot("10:00", "10:30", "把门口的鞋摆整齐"),
            slot("10:30", "11:00", "把碗洗了"),
        ]
        self.assertEqual([s["event"] for s in clean_slots(slots, kind="pony")], ["把碗洗了"])

    def test_purge_removes_it_from_the_state(self):
        service = self.build()
        service._scope.update_self(
            daily_schedule=[
                slot("10:00", "10:30", "把门口的鞋摆整齐"),
                slot("10:30", "11:00", "把碗洗了"),
            ]
        )
        service._purge_dirty_history()
        left = [s["event"] for s in service._scope.get_self("daily_schedule")]
        self.assertEqual(left, ["把碗洗了"])
        self.assertEqual(service._scope.get_self(PURGE_KEY), PURGE_VERSION)
        self.assertEqual(PURGE_VERSION, 7)

    def test_purge_keeps_everything_for_a_human(self):
        service = self.build(species=HUMAN)
        service._scope.update_self(
            daily_schedule=[slot("10:00", "10:30", "把门口的鞋摆整齐")]
        )
        service._purge_dirty_history()
        self.assertEqual(
            [s["event"] for s in service._scope.get_self("daily_schedule")],
            ["把门口的鞋摆整齐"],
        )

    def test_the_quality_gate_knows_the_body(self):
        service = self.build()
        issues, _ = service._quality_issues(
            slot("10:00", "10:30", "把门口的鞋摆整齐"),
            "",
            None,
            service._species_kind(),
        )
        self.assertIn("物种:鞋", issues)


class PromptTest(unittest.TestCase):
    """提示词里得把「这具身体」和「她多大」说清楚，没档案时一个字都不多。"""

    def test_the_body_and_the_age_reach_the_prompt(self):
        text = segment_prompt(
            cfg(),
            now_text="13:10",
            weekday="星期六",
            species="她是天马（角、翅膀、四蹄）。没有手指、没有鞋，做事用蹄子、嘴或魔法。",
            basics="名字是茉心，今年 23 岁，生日 4 月 9 日。",
        )
        self.assertIn("她的身体", text)
        self.assertIn("没有鞋", text)
        self.assertIn("她的基本信息", text)
        self.assertIn("23 岁", text)

    def test_without_a_profile_the_prompt_is_unchanged(self):
        text = segment_prompt(cfg(), now_text="13:10", weekday="星期六")
        self.assertNotIn("她的身体", text)
        self.assertNotIn("她的基本信息", text)


class RoleBasicsTest(unittest.TestCase):
    """基础信息：只从人设里读，年龄按生日现算——跨年自己长，不靠改状态。"""

    def test_it_reads_name_age_and_birthday(self):
        basics = role_basics.parse_basics(
            "名字叫作茉心，今年 23 岁，生日 4 月 9 日。", today=FIXED_TODAY
        )
        self.assertEqual(basics["name"], "茉心")
        self.assertEqual(basics["age"], 23)
        self.assertEqual(basics["age_as_of"], FIXED_TODAY)
        self.assertEqual(basics["birthday"], "04-09")

    def test_age_grows_by_itself(self):
        basics = role_basics.parse_basics("今年 23 岁", today=FIXED_TODAY)
        # 没过周年 → 还是 23；过了周年 → 24；再过四年 → 27。
        self.assertEqual(role_basics.age_at(basics, "2027-08-21"), 23)
        self.assertEqual(role_basics.age_at(basics, "2027-08-22"), 24)
        self.assertEqual(role_basics.age_at(basics, "2030-08-22"), 27)

    def test_a_birth_year_beats_a_stored_age(self):
        basics = role_basics.parse_basics("她出生于 2003 年 4 月 9 日。", today=FIXED_TODAY)
        self.assertEqual(basics["birth_year"], 2003)
        self.assertNotIn("age", basics)
        self.assertEqual(role_basics.age_at(basics, "2026-04-08"), 22)
        self.assertEqual(role_basics.age_at(basics, "2026-04-09"), 23)

    def test_nothing_is_invented(self):
        self.assertEqual(role_basics.parse_basics("她喜欢下雨天。", today=FIXED_TODAY), {})
        self.assertEqual(role_basics.render_basics({}, FIXED_TODAY), "")

    def test_merge_only_fills_blanks(self):
        current = {"age": 30, "name": ""}
        merged = role_basics.merge_missing(current, {"age": 23, "name": "茉心"})
        self.assertEqual(merged, {"age": 30, "name": "茉心"})

    def test_the_service_stores_it_once_and_renders_it(self):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(datetime(2026, 8, 22, 13, 10, tzinfo=TZ)),
            logger=RecordingLogger(),
            monotonic=lambda: 10_000.0,
        )
        persona = Persona(name="茉心", prompt="茉心，23 岁，生日 4 月 9 日。", source="t")
        stored = service._ensure_basics(persona)
        self.assertEqual(stored["age"], 23)
        self.assertEqual(service._scope.get_self("basics")["birthday"], "04-09")
        self.assertIn("23 岁", service._basics_text())


class CoreBasicsTest(unittest.TestCase):
    """core 上那份也得能读：面板/契约走的是 `core.basics_profile()`。"""

    def test_it_parses_persona_once_and_computes_the_age(self):
        import asyncio
        import tempfile
        from pathlib import Path

        from humanoid.config import HumanoidConfig
        from humanoid.core_instance import HumanoidCoreInstance
        from humanoid.state import StateStore

        from .fakes import FakeContext

        conf = HumanoidConfig.from_raw({"timezone_city": "北京"})
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 1.0)
        store.load("2026-08-22", 28)
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: conf,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        core._ccb_persona_prompt = lambda: "名字叫茉心，23 岁，生日 4 月 9 日。"
        info = core.basics_profile()
        self.assertEqual(info["name"], "茉心")
        self.assertEqual(info["birthday"], "04-09")
        self.assertEqual(info["age"], 23)
        # 写进状态之后第二次读不再依赖人设正文。
        core._ccb_persona_prompt = lambda: ""
        self.assertEqual(core.basics_profile()["age"], 23)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
