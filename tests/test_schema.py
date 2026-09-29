"""守护 `_conf_schema.json` 与 `HumanoidConfig` 的一致性。

两边一旦漂移，用户在 WebUI 看到的默认值就会和插件实际使用的不一样。
默认值的唯一来源是 `HumanoidConfig`，schema 只负责展示。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from humanoid.config import DEFAULTS, GRANULARITY_MINUTES, HumanoidConfig

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


class SchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_is_valid_json_object(self):
        self.assertIsInstance(self.schema, dict)
        self.assertGreater(len(self.schema), 50)

    def test_keys_match_config_fields_exactly(self):
        fields = set(DEFAULTS.__dataclass_fields__)
        keys = set(self.schema)
        self.assertEqual(keys - fields, set(), "schema 里有配置类不认识的键")
        self.assertEqual(fields - keys, set(), "配置类有 schema 里没暴露的键")

    def test_every_item_has_description_type_default(self):
        for key, meta in self.schema.items():
            with self.subTest(key=key):
                self.assertIn("description", meta)
                self.assertIn("type", meta)
                self.assertIn("default", meta)
                self.assertIn(meta["type"], {"string", "int", "float", "bool", "list", "object"})

    def test_defaults_round_trip_to_config_defaults(self):
        built = HumanoidConfig.from_raw({k: v["default"] for k, v in self.schema.items()})
        for name in DEFAULTS.__dataclass_fields__:
            with self.subTest(field=name):
                self.assertEqual(getattr(built, name), getattr(DEFAULTS, name))

    def test_provider_selectors_use_the_real_special_key(self):
        expected = {
            "schedule_provider_name",
            "schedule_fallback_provider_name",
            "mood_provider_name",
        }
        found = {k for k, v in self.schema.items() if v.get("_special") == "select_provider"}
        self.assertEqual(found, expected)

    def test_global_fallback_defaults_to_on(self):
        self.assertTrue(self.schema["schedule_allow_global_fallback"]["default"])
        self.assertTrue(DEFAULTS.schedule_allow_global_fallback)

    def test_granularity_options_match_config_table(self):
        options = set(self.schema["schedule_time_granularity"]["options"])
        self.assertEqual(options, set(GRANULARITY_MINUTES))

    def test_numeric_bounds_do_not_contradict_defaults(self):
        for key, meta in self.schema.items():
            if meta["type"] not in {"int", "float"}:
                continue
            with self.subTest(key=key):
                value = meta["default"]
                if "minimum" in meta:
                    self.assertGreaterEqual(value, meta["minimum"])
                if "maximum" in meta:
                    self.assertLessEqual(value, meta["maximum"])

    def test_new_keys_are_present(self):
        for key in (
            "schedule_llm_timeout_seconds",
            "schedule_generation_max_attempts",
            "schedule_max_slots",
            "schedule_provider_cooldown_minutes",
            "mood_provider_name",
            "state_flush_interval_seconds",
        ):
            self.assertIn(key, self.schema)


class DefaultMigrationTest(unittest.TestCase):
    """AstrBot 只补缺不覆盖，所以旧默认值必须靠插件自己提升一次。"""

    def test_legacy_weather_location_is_dropped_when_city_changed(self):
        from humanoid.config import plan_default_migrations

        changes = plan_default_migrations({
            "timezone_city": "北京",
            "weather_location": "Heyuan,CN",
        })
        self.assertEqual(changes, {"weather_location": ""})

    def test_user_chosen_values_are_never_touched(self):
        from humanoid.config import plan_default_migrations

        # 自己填了天气城市：不碰
        self.assertEqual(
            plan_default_migrations({"timezone_city": "北京", "weather_location": "Tokyo,JP"}), {}
        )
        # 城市还是占位默认值：留着 Heyuan,CN 比拿不到天气好
        self.assertEqual(
            plan_default_migrations({
                "timezone_city": "河源（记得改~）", "weather_location": "Heyuan,CN",
            }),
            {},
        )

    def test_migration_is_idempotent(self):
        from humanoid.config import plan_default_migrations

        self.assertEqual(plan_default_migrations({"timezone_city": "北京", "weather_location": ""}), {})


class MetadataTest(unittest.TestCase):
    def test_version_matches_package(self):
        from humanoid import __version__

        text = (SCHEMA_PATH.parent / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn(f"version: {__version__}", text)


if __name__ == "__main__":
    unittest.main()


class ReadmeCommandsMatchCode(unittest.TestCase):
    """README 里的指令表必须和**实际注册**的一致。

    v2.24.2 修了这一处：v2.x 把 8 个常用指令从 `/拟人` 组里拆成了顶层命令
    （`/你的状态` 而不是 `/拟人 状态`），但 README 那张表没跟着改——照着 README 打的
    **一条都执行不了**，而且命令名本身也全错（`状态` vs `你的状态`、
    `日程` vs `查看日程`、`帮助` vs `拟人帮助`）。

    文档漂移是这类项目最容易反复发作的问题：改了代码忘了改文档，或者反过来。
    这里直接从 `main.py` 里解析出真实的注册名，反查 README 有没有漏。
    """

    def _registered(self) -> tuple:
        import re
        src = (SCHEMA_PATH.parent / "main.py").read_text(encoding="utf-8")
        top = set(re.findall(r'@filter\.command\("([^"]+)"\)', src))
        grouped = set(re.findall(r'@humanoid_group\.command\("([^"]+)"\)', src))
        return top, grouped

    def test_readme_lists_every_registered_command(self):
        top, grouped = self._registered()
        self.assertTrue(top and grouped, "从 main.py 解析不到命令，测试本身失效了")
        readme = (SCHEMA_PATH.parent / "README.md").read_text(encoding="utf-8")
        # 顶部那张表是「不带前缀」的区段：从标题到管理员表为止。
        head = readme.split("**管理员 · 改配置**")[0]
        for name in top:
            self.assertIn(f"/{name}", head,
                          f"README 顶部表里没有顶层指令 /{name}（实际已注册）")
        for name in grouped:
            self.assertIn(f"/拟人 {name}", readme,
                          f"README 里没有管理员指令 /拟人 {name}（实际已注册）")

    def test_readme_does_not_promise_removed_prefixes(self):
        """不能出现 `/拟人 状态` 这种已经拆掉的写法（历史说明那一行除外）。"""
        readme = (SCHEMA_PATH.parent / "README.md").read_text(encoding="utf-8")
        bad = re.findall(r"`/拟人 (状态|日程|时间|好感度|情绪详情|情绪日志|叫我|帮助)`", readme)
        self.assertEqual(bad, [], f"README 仍写着已拆掉的旧指令：{bad}")
