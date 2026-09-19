"""向后兼容：v2.10.2 的 state.json 必须能被 v2.11.0 无损加载。

这里的样例 state 按 v2.10.2 `init_default_state()` / `_ensure_state_fields()` 的
真实字段构造，包含它写过的所有键（含只写不读的 `_energy_noise_date`）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.engine import HumanoidEngine
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.role_manager import RoleManager
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
    "user_last_seen": {"3881756548": 1_780_000_000.0, "10086": 1_779_000_000.0},
    "last_message": {
        "3881756548": {"text": "那我先睡了", "timestamp": 1_780_000_000.0}
    },
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
        """旧版顶层字段必须完整搬进 default 角色，而不是停在顶层。"""
        role = self.load().data["roles"]["default"]
        self_state = role["self"]
        users = role["users"]
        self.assertEqual(users["3881756548"]["nickname"], LEGACY_STATE["nicknames"]["3881756548"])
        self.assertEqual(users["10086"]["nickname"], LEGACY_STATE["nicknames"]["10086"])
        self.assertEqual(users["3881756548"]["mood"], LEGACY_STATE["moods"]["3881756548"])
        self.assertEqual(users["3881756548"]["mood_logs"], LEGACY_STATE["mood_logs"]["3881756548"])
        self.assertEqual(
            users["3881756548"]["mood_tag"], LEGACY_STATE["mood_tags"]["3881756548"]
        )
        self.assertEqual(
            users["3881756548"]["last_interaction"],
            LEGACY_STATE["user_last_seen"]["3881756548"],
        )
        self.assertEqual(
            users["3881756548"]["last_message"]["text"],
            LEGACY_STATE["last_message"]["3881756548"]["text"],
        )
        self.assertEqual(self_state["energy"], 63.4)
        self.assertEqual(self_state["social_energy"], 44.0)
        self.assertEqual(self_state["current_cycle_day"], 17)
        self.assertEqual(self_state["_cached_weather_obj"], LEGACY_STATE["_cached_weather_obj"])
        self.assertEqual(self_state["last_update"], "2026-08-20 21:14:07")

    def test_legacy_top_level_is_not_duplicated(self):
        """迁移后顶层不能再留角色数据的拷贝：多角色下那份只属于第一个角色。"""
        data = self.load().data
        for key in ("energy", "social_energy", "moods", "nicknames", "mood_logs", "mood_tags"):
            self.assertNotIn(key, data, f"{key} 应该已经并入 roles[default]")

    def test_new_fields_are_added(self):
        data = self.load().data
        self.assertEqual(data["_state_version"], STATE_VERSION)
        self_state = data["roles"]["default"]["self"]
        self.assertIn("daily_schedule", self_state)
        self.assertIn("today_date", self_state)
        self.assertIn("current_cycle_day", self_state)

    def test_zombie_field_dropped(self):
        data = self.load().data
        self.assertNotIn("_energy_noise_date", data)
        self.assertNotIn("_energy_noise_date", data["roles"]["default"]["self"])

    def test_social_energy_migrates_into_default_role(self):
        data = self.load().data
        self.assertEqual(data["roles"]["default"]["self"]["social_energy"], 44.0)

    def test_no_corrupt_backup_created(self):
        store = self.load()
        backups = list(store.path.parent.glob("*.corrupt-*.json"))
        self.assertEqual(backups, [], "正常的老文件不应被当成损坏文件备份")


class LegacyEngineTest(unittest.IsolatedAsyncioTestCase):
    """老 state.json 必须能被现在的装配路径直接读出来，不要求用户重置状态。"""

    async def test_engine_boots_on_legacy_state(self):
        datadir = Path(tempfile.mkdtemp())
        (datadir / "state.json").write_text(
            json.dumps(LEGACY_STATE, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        conf = HumanoidConfig.from_raw({"timezone_city": "北京"})
        log = RecordingLogger()
        store = StateStore(datadir / "state.json", lambda: 1.0, log)
        store.load("2026-08-22", conf.cycle_length)
        ctx = FakeContext()
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: conf, log)
        roles = RoleManager(store, lambda: conf, log, resolver, gateway, None)
        engine = HumanoidEngine(ctx, {"timezone_city": "北京"}, datadir, log, None, roles)
        await roles.start()
        core = roles.get_or_create("default")
        try:
            self.assertEqual(core.mood.nickname("3881756548"), "娜莉灵")
            profile = core.mood.profile("3881756548")
            self.assertEqual(profile["turn_count"], 142)
            self.assertAlmostEqual(profile["base_affection"], 60.0)
            self.assertIn("last_decay", profile, "老档案缺的字段应被补齐")
            self.assertEqual(core.mood.tag("3881756548"), "状态平稳，开心，亲切")

            core.schedule.seed_first_segment()
            snap = core.snapshot("3881756548")
            self.assertTrue(snap["today"])
            self.assertTrue(snap["schedule"]["slots"], "老档案启动后也要有当下这一段")
            self.assertLessEqual(snap["energy"]["value"], snap["energy"]["max"])
            self.assertIn("角色：default", "\n".join(core.status_lines("3881756548")))
            self.assertIn("好感度上升至 78.5", core.mood.logs_text("3881756548"))
        finally:
            await roles.stop()
            await store.stop()  # 必须显式落盘，否则文件里还是迁移前的旧结构

        saved = json.loads((datadir / "state.json").read_text(encoding="utf-8"))
        users = saved["roles"]["default"]["users"]
        self.assertEqual(users["10086"]["nickname"], "小十")
        self.assertEqual(users["3881756548"]["nickname"], "娜莉灵")
        self.assertEqual(saved["_state_version"], STATE_VERSION)


if __name__ == "__main__":
    unittest.main()
