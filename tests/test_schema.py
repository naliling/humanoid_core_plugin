"""守护 `_conf_schema.json` 与 `HumanoidConfig` 的一致性。

两边一旦漂移，用户在 WebUI 看到的默认值就会和插件实际使用的不一样。
默认值的唯一来源是 `HumanoidConfig`，schema 只负责展示。
"""

from __future__ import annotations

import json
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


class MetadataTest(unittest.TestCase):
    def test_version_matches_package(self):
        from humanoid import __version__

        text = (SCHEMA_PATH.parent / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn(f"version: {__version__}", text)


if __name__ == "__main__":
    unittest.main()
