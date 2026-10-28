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
            "input_state_min_interval_seconds",
            "input_state_max_frames",
        ):
            self.assertIn(key, self.schema)

    def test_field_count_is_pinned(self):
        """字段数变化必须同步改这里（v3.0.0 = 134：新增「正在输入」节流两项）。

        面板项数在 README / main.py 的帮助文案里也被引用，字段增删时一并对齐，
        否则用户看到的「共 N 项」与面板实际数量对不上。
        """
        self.assertEqual(len(DEFAULTS.__dataclass_fields__), 134)
        self.assertEqual(len(self.schema), 134)

    def test_input_state_throttle_keys_are_consistent(self):
        """v3.0.0 新增的两个「正在输入」节流项：类型/区间/默认值三处一致。"""
        interval = self.schema["input_state_min_interval_seconds"]
        self.assertEqual(interval["type"], "float")
        self.assertEqual(interval["default"], 0.3)
        self.assertEqual((interval["minimum"], interval["maximum"]), (0.1, 10.0))
        frames = self.schema["input_state_max_frames"]
        self.assertEqual(frames["type"], "int")
        self.assertEqual(frames["default"], 60)
        self.assertEqual((frames["minimum"], frames["maximum"]), (5, 500))
        built = HumanoidConfig.from_raw({
            "input_state_min_interval_seconds": 0.3,
            "input_state_max_frames": 60,
        })
        self.assertEqual(built.input_state_min_interval_seconds, 0.3)
        self.assertEqual(built.input_state_max_frames, 60)


class SliderKeyTest(unittest.TestCase):
    """钉住「区间声明」与「面板真正认的键」不许漂移。

    AstrBot 的配置渲染器（`webui/assets/ProviderSelectMenu-*.js`）在写回配置时
    收到的是 `const q = itemMeta?.slider; if (q) return Math.max(q.min ?? 0,
    Math.min(q.max ?? 100, v)); return v` —— **没有 slider 就原样放行**。
    `minimum`/`maximum` 前端一个都不读，后端也不校验。

    所以区间必须同时落在这两处：`minimum`/`maximum`（本仓库把这里当唯一真源）
    与 `slider`。任何一边单独改动，都会让「面板显示的范围」与「实际生效的范围」
    对不上，而面板还会悄悄放行越界值。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _ranged(self):
        for key, meta in self.schema.items():
            if meta.get("type") not in {"int", "float"}:
                continue
            if not isinstance(meta.get("minimum"), (int, float)):
                continue
            if not isinstance(meta.get("maximum"), (int, float)):
                continue
            yield key, meta

    def test_every_declared_range_also_reaches_the_panel(self):
        missing = [key for key, meta in self._ranged() if "slider" not in meta]
        self.assertEqual(missing, [], "声明了区间却没有 slider，面板根本不钳制")

    def test_slider_matches_the_declared_range(self):
        for key, meta in self._ranged():
            with self.subTest(key=key):
                slider = meta.get("slider")
                self.assertIsInstance(slider, dict)
                self.assertEqual(set(slider), {"min", "max", "step"})
                self.assertEqual(slider["min"], meta["minimum"])
                self.assertEqual(slider["max"], meta["maximum"])

    def test_range_is_wide_enough_to_slide(self):
        for key, meta in self._ranged():
            with self.subTest(key=key):
                self.assertLess(
                    meta["minimum"], meta["maximum"], "区间退化成一个点，滑块没有意义"
                )

    def test_slider_step_is_usable(self):
        for key, meta in self._ranged():
            with self.subTest(key=key):
                slider = meta["slider"]
                span = meta["maximum"] - meta["minimum"]
                self.assertGreater(slider["step"], 0)
                self.assertLessEqual(slider["step"], span)
                if meta["type"] == "int":
                    self.assertEqual(
                        slider["step"],
                        int(slider["step"]),
                        "int 项的滑块步长必须还是整数，否则滑块会产出一串小数",
                    )

    def test_default_sits_inside_the_slider(self):
        for key, meta in self._ranged():
            value = meta.get("default")
            if not isinstance(value, (int, float)):
                continue
            with self.subTest(key=key):
                slider = meta["slider"]
                self.assertGreaterEqual(value, slider["min"])
                self.assertLessEqual(value, slider["max"])


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
        # 「时间」不在列表里：v2.28.11 起 `/拟人 时间 城市` 是**真的**管理指令
        # （设置她的城市）；`/时间 城市` 仍然是所有人都能用的查询。
        bad = re.findall(r"`/拟人 (状态|日程|好感度|情绪详情|情绪日志|叫我|帮助)`", readme)
        self.assertEqual(bad, [], f"README 仍写着已拆掉的旧指令：{bad}")


class SchemaHintsAndBoundsTest(unittest.TestCase):
    """hint / minimum / maximum 必须与代码实际行为一致。

    面板显示的值与代码生效的值是两份时，用户没有任何线索能发现——所以这几条
    不是文案洁癖，是「用户看到的范围必须是他真能拿到的范围」。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = SCHEMA_PATH.read_text(encoding="utf-8")
        cls.schema = json.loads(cls.raw)

    def test_no_replacement_characters_anywhere(self):
        """`admin_qq` 的 hint 里曾有 3 个 U+FFFD（一个汉字的 UTF-8 三字节被打坏）。"""
        self.assertNotIn("\ufffd", self.raw, "schema 里还有乱码替换字符")

    def test_admin_qq_hint_is_readable_chinese(self):
        self.assertEqual(self.schema["admin_qq"]["hint"], "能发管理指令的 QQ 名单")

    def test_floors_match_the_code_clamps(self):
        """0 冻住全部情绪、1 关掉「连续才扣好感」：两者都曾是面板上的合法值。"""
        for key, floor in (("mood_sensitivity", 1), ("mood_negative_streak_required", 2)):
            with self.subTest(key=key):
                self.assertEqual(self.schema[key]["minimum"], floor)
                self.assertEqual(getattr(HumanoidConfig.from_raw({key: floor}), key), floor)

    def test_rate_field_maxima_match_the_hint_magnitude(self):
        """hint 写 0.05~1.0 而 maximum 是 100：面板放行、代码原样采用，hint 就是假的。"""
        for key, top in (
            ("social_energy_consumption_per_msg", 1.0),
            ("social_energy_recovery_per_minute", 3.0),
            ("energy_natural_recovery_per_minute", 0.9),
            ("energy_consumption_per_msg", 0.9),
        ):
            with self.subTest(key=key):
                self.assertEqual(self.schema[key]["maximum"], top)
                self.assertEqual(getattr(HumanoidConfig.from_raw({key: top}), key), top)

    def test_night_window_hints_explain_equal_hours(self):
        """起止相同 = 没有夜里（不是「全天算夜里」），面板必须说出来。"""
        for key in ("night_start_hour", "night_end_hour"):
            with self.subTest(key=key):
                self.assertIn("相同", self.schema[key]["hint"])
                self.assertIn("关闭夜间窗口", self.schema[key]["hint"])

    def test_affection_override_hint_teaches_one_entry_per_line(self):
        """list 编辑器一个条目就是一条：教「一个条目里用逗号分隔」会让两条一起失效。"""
        hint = self.schema["mood_affection_override"]["hint"]
        self.assertIn("每个条目", hint)
        self.assertNotIn("逗号分隔", hint)
