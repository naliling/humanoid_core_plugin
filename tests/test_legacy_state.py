"""向后兼容：v2.10.2 的 state.json 必须能被 v2.11.0 无损加载。

这里的样例 state 按 v2.10.2 `init_default_state()` / `_ensure_state_fields()` 的
真实字段构造，包含它写过的所有键（含只写不读的 `_energy_noise_date`）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from humanoid.engine import HumanoidEngine
from humanoid.state import STATE_VERSION, StateStore

from .fakes import FakeContext, RecordingLogger

LEGACY_STATE = {
    "energy": 63.4,
    "current_cycle_day": 17,
    "last_cycle_update": "2026-08-20",
    "last_update": "2026-08-20 21:14:07",
    "today_date": "2026-08-20",
    "daily_schedule": [
        {
            "start": "00:00",
            "end": "07:30",
            "event": "深度睡眠",
            "location": "卧室",
            "emotion": "沉睡/安详",
            "energy_rate": 0.15,
        },
        {
            "start": "07:30",
            "end": "24:00",
            "event": "白天活动",
            "location": "家中",
            "emotion": "平稳",
            "energy_rate": -0.05,
        },
    ],
    "_cached_weather_obj": {"weather": "晴 🌡️ 31°C", "env": "当前城市 [Heyuan,CN] 天气：晴"},
    "_last_weather_fetch": "2026-08-20 20:00:00",
    "_cached_location": "Heyuan,CN",
    "nicknames": {"3881756548": "娜莉灵", "10086": "小十"},
    "_energy_noise_date": "2026-08-20",
    "moods": {
        "3881756548": {
            "affection": 78.5,
            "libido": 41.2,
            "aggression": 6.0,
            "base_affection": 60.0,
            "base_libido": 36.0,
            "base_aggression": 20.0,
            "last_interaction": 1_780_000_000.0,
            "turn_count": 142,
        }
    },
    "_mood_decay_last_run": 1_780_000_100.0,
    "mood_logs": {
        "3881756548": [
            {
                "time": "2026-08-20 19:02:11",
                "event": "好感度上升至 78.5",
                "affection": 78.5,
                "libido": 41.2,
                "aggression": 6.0,
            }
        ]
    },
    "social_energy": 44.0,
    "mood_tags": {"3881756548": "状态平稳，开心，亲切"},
    "_last_social_energy_reset_date": "2026-08-20",
}


class LegacyStateTest(unittest.TestCase):
    def load(self) -> StateStore:
        path = Path(tempfile.mkdtemp()) / "state.json"
        path.write_text(json.dumps(LEGACY_STATE, ensure_ascii=False, indent=4), encoding="utf-8")
        store = StateStore(path, lambda: 1.0, RecordingLogger())
        store.load("2026-08-22", 28)
        return store

    def test_user_data_survives_intact(self):
        data = self.load().data
        self.assertEqual(data["nicknames"], LEGACY_STATE["nicknames"])
        self.assertEqual(data["moods"], LEGACY_STATE["moods"])
        self.assertEqual(data["mood_logs"], LEGACY_STATE["mood_logs"])
        self.assertEqual(data["mood_tags"], LEGACY_STATE["mood_tags"])
        self.assertEqual(data["energy"], 63.4)
        self.assertEqual(data["social_energy"], 44.0)
        self.assertEqual(data["current_cycle_day"], 17)
        self.assertEqual(data["_cached_weather_obj"], LEGACY_STATE["_cached_weather_obj"])
        self.assertEqual(data["last_update"], "2026-08-20 21:14:07")

    def test_new_fields_are_added(self):
        data = self.load().data
        self.assertEqual(data["_state_version"], STATE_VERSION)
        self.assertIn("schedule_source", data)
        self.assertIn("schedule_generated_at", data)
        self.assertIn("_schema_migrated_to", data)

    def test_zombie_field_dropped(self):
        self.assertNotIn("_energy_noise_date", self.load().data)

    def test_no_corrupt_backup_created(self):
        store = self.load()
        backups = list(store.path.parent.glob("*.corrupt-*.json"))
        self.assertEqual(backups, [], "正常的老文件不应被当成损坏文件备份")


class LegacyEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_engine_boots_on_legacy_state(self):
        datadir = Path(tempfile.mkdtemp())
        (datadir / "state.json").write_text(
            json.dumps(LEGACY_STATE, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        engine = HumanoidEngine(
            FakeContext(), {"timezone_city": "北京"}, datadir, RecordingLogger()
        )
        await engine.start()
        try:
            # 老档案里的昵称与情绪必须还在
            self.assertEqual(engine.nickname("3881756548"), "娜莉灵")
            profile = engine.mood.profile("3881756548")
            self.assertEqual(profile["turn_count"], 142)
            self.assertAlmostEqual(profile["base_affection"], 60.0)
            self.assertIn("last_decay", profile, "老档案缺的字段应被补齐")

            # 跨天：日程会换成今天的，但历史数据不受影响
            snap = engine.snapshot(advance_energy=True)
            self.assertEqual(snap.today, engine.clock.today_str())
            self.assertTrue(snap.schedule)
            self.assertLessEqual(snap.energy, snap.max_energy)

            # 指令文本都能正常生成
            self.assertIn("当前状态", "\n".join(engine.status_lines("3881756548")))
            self.assertIn("好感度上升至 78.5", engine.mood_log_text("3881756548"))
        finally:
            await engine.stop()

        saved = json.loads((datadir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["nicknames"]["10086"], "小十")
        self.assertEqual(saved["_state_version"], STATE_VERSION)


if __name__ == "__main__":
    unittest.main()
