"""v3.1.35：重启后还记得「隔了多久」、占位符不再进她的账本、清理升到 v8。

三件事都来自线上真实数据：

* `spoke_log` 里躺着 `(Model response will be rendered here)`——AstrBot 分段流式
  渲染时先填的占位文本，被 `note_her_reply` 当成她的原话记了账，还会进情绪分析。
* `materials_done` 里攒出「今天上午今天上午今天上午凌晨…」（`past_title()` 不幂等，
  每调一次再拼一个前缀）和「把门口的鞋摆整齐，顺手擦了鞋柜」（素材池照人写的）。
* `_last_interaction` 只在内存里：AstrBot 一重启，所有角色都变成「从没人跟她说过
  话」，`idle_active=True`，日程生成被空闲静默整段挡掉。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import CallGate
from humanoid.reply_guard import is_placeholder
from humanoid.services.schedule import (
    PURGE_KEY,
    PURGE_VERSION,
    ScheduleService,
)
from .fakes import FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"

#: 线上账本里那条占位文本的原样。
PLACEHOLDER = "(Model response will be rendered here)"


class PlaceholderTest(unittest.TestCase):
    """占位符只认死证据，别把她的话误判掉。"""

    def test_the_render_placeholder_is_recognised(self):
        self.assertTrue(is_placeholder(PLACEHOLDER))
        self.assertTrue(is_placeholder("  (Model response will be rendered here) "))

    def test_a_real_sentence_is_not_a_placeholder(self):
        self.assertFalse(is_placeholder("我在厨房呢，刚把碗洗完。"))
        self.assertFalse(is_placeholder(""))
        self.assertFalse(is_placeholder("   "))


class _Base(unittest.TestCase):
    moment = datetime(2026, 8, 22, 13, 10, tzinfo=TZ)

    def build(self, moment=None):
        conf = cfg(
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment or self.moment),
            logger=RecordingLogger(),
            monotonic=lambda: 10_000.0,
        )
        # 非人形角色：「鞋柜」这类词才算不该出现（人类角色本来就该穿鞋）。
        service._scope.set_self(
            "species",
            {"kind": "pony", "label": "天马", "traits": ["角", "四蹄"], "confidence": 0.9},
        )
        return service


class PurgeV8Test(_Base):
    """`materials_done` 与 `spoke_log` 里的脏行要真删，干净行要留。"""

    def test_dirty_done_rows_are_purged(self):
        service = self.build()
        service._scope.set_self(
            "materials_done",
            [
                {"title": "把门口的鞋摆整齐，顺手擦了鞋柜", "slot": "上午", "done_at": 1.0},
                {"title": "今天上午今天上午今天上午凌晨在被小腹的坠痛弄醒", "slot": "凌晨", "done_at": 2.0},
                {"title": "把稿子改完了", "slot": "下午", "done_at": 3.0},
            ],
        )
        service._purge_dirty_history()
        rows = service._scope.get_self("materials_done") or []
        self.assertEqual([row.get("title") for row in rows], ["把稿子改完了"])
        self.assertEqual(service._scope.get_self(PURGE_KEY), PURGE_VERSION)

    def test_placeholder_spoke_rows_are_purged(self):
        service = self.build()
        service._scope.set_user(
            "1956758925",
            "spoke_log",
            [
                {"text": PLACEHOLDER, "at": 1794257748.7, "pending": False, "landed": True},
                {"text": "我也爱你，尘心。", "at": 1794257800.0, "pending": False},
            ],
        )
        service._purge_dirty_history()
        rows = service._scope.peek_user("1956758925", "spoke_log") or []
        self.assertEqual([row.get("text") for row in rows], ["我也爱你，尘心。"])

    def test_v8_is_recorded_so_it_runs_once(self):
        service = self.build()
        service._scope.set_self(
            "materials_done",
            [{"title": "把门口的鞋摆整齐，顺手擦了鞋柜", "slot": "上午", "done_at": 1.0}],
        )
        self.assertEqual(service._purge_dirty_history(), 1)
        self.assertEqual(service._purge_dirty_history(), 0)


class IdlePersistenceTest(unittest.TestCase):
    """「隔了多久」要能扛住重启。"""

    def test_a_restart_keeps_how_long_since_someone_spoke(self):
        path = Path(tempfile.mkdtemp()) / "llm_interactions.json"
        conf = cfg()
        wall = lambda: 1_000_000.0  # noqa: E731
        first = CallGate(lambda: conf, wall_clock=wall, monotonic=lambda: 0.0)
        first.bind_interactions(path)
        first.note_interaction(999_400.0, role_id="3951331639")

        # 重启：新实例内存是空的，只能从文件里恢复。
        second = CallGate(lambda: conf, wall_clock=wall, monotonic=lambda: 0.0)
        self.assertIsNone(second.interaction_at("3951331639"))
        second.bind_interactions(path)
        self.assertEqual(second.interaction_at("3951331639"), 999_400.0)

    def test_no_path_still_works_in_memory(self):
        conf = cfg()
        gate = CallGate(lambda: conf, wall_clock=lambda: 1_000_000.0, monotonic=lambda: 0.0)
        gate.note_interaction(999_000.0, role_id="default")
        self.assertEqual(gate.interaction_at("default"), 999_000.0)

    def test_a_future_stamp_is_ignored(self):
        path = Path(tempfile.mkdtemp()) / "llm_interactions.json"
        path.write_text('{"default": 9999999999.0}', encoding="utf-8")
        conf = cfg()
        gate = CallGate(
            lambda: conf, wall_clock=lambda: 1_000_000.0, monotonic=lambda: 0.0
        )
        gate.bind_interactions(path)
        self.assertIsNone(gate.interaction_at("default"))


class SleepIsNotForcedTest(_Base):
    """困意只在提示词里当建议，不在这里把模型给的段改成睡（v3.1.36）。"""

    def test_no_forced_sleep_helper_exists(self):
        service = self.build(datetime(2026, 8, 22, 2, 30, tzinfo=TZ))
        self.assertFalse(hasattr(service, "_sleep_overrules"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
