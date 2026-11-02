"""守护 `_conf_schema.json` 里的 `condition`：子项必须跟着主开关显示。

为什么要有这个测试：面板项一共 135 项，从上往下翻半天找不到想改的那一项，
是用户实际抱怨过的体验问题。AstrBot 的前端（`webui/assets/ProviderSelectMenu-*.js`）
会读 itemMeta 上的 `condition`：`function z(T,D){if(!(T!=null&&T.condition))return!0;
for(const[M,P]of Object.entries(T.condition))if(I(i.iterable,M)!==P)return!1;return!0}`
——`I` 按点号路径在当前配置上取值，用严格 `!==` 比较，全部命中才显示该项。

所以 `condition` 写错一个字，结果不是「多显示一项」，而是**那一项永久消失**
（用户再也改不到它）。这里钉住三条：
1. 每个 `ccb_*` 都必须挂在 `ccb` 主开关下；
2. 条件里引用的键必须真实存在，且比较值的类型与该项默认值一致
   （`!==` 比较下 `true` 与 `"true"` 不相等，写错类型等于永久隐藏）；
3. 条件键不能成环，也不能把主开关自己藏起来。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

# 面板上的主开关：它们必须始终可见，否则整组功能无从打开。
# （`mood_use_llm_for_delta` / `mood_log_enabled` / `cooling_enabled` 不在这里：
#  它们是「情绪」组里的二级开关，跟着 mood_enabled 隐藏是对的。）
MASTER_SWITCHES = (
    "ccb",
    "mood_enabled",
    "use_llm_schedule",
    "weather_enabled",
    "outfit_enabled",
    "input_state_enabled",
    "social_energy_enabled",
    "night_mode_enabled",
    "soma_enabled",
    "enable_cycle",
    "enable_energy_natural_recovery",
)

# 开关打开才有意义的子项：主开关关着时这些项在面板上不该出现。
FOLLOWERS = {
    "ccb_affection_min": "ccb",
    "ccb_allowed_users": "ccb",
    "mood_sensitivity": "mood_enabled",
    "mood_initial_affection": "mood_enabled",
    "cooling_minutes": "cooling_enabled",
    "mood_provider_name": "mood_use_llm_for_delta",
    "mood_log_max_entries": "mood_log_enabled",
    "schedule_provider_name": "use_llm_schedule",
    "weather_api_key": "weather_enabled",
    "outfit_preset": "outfit_enabled",
    "input_state_max_frames": "input_state_enabled",
    "social_energy_reset_hour": "social_energy_enabled",
    "night_start_hour": "night_mode_enabled",
    "body_tick_seconds": "soma_enabled",
    "cycle_length": "enable_cycle",
    "energy_natural_recovery_per_minute": "enable_energy_natural_recovery",
}


class SchemaConditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_every_ccb_field_follows_the_master_switch(self) -> None:
        followers = [k for k in self.schema if k.startswith("ccb_")]
        self.assertGreaterEqual(len(followers), 15, "ccb_* 项少了一半，测试本身失效了")
        for key in followers:
            with self.subTest(key=key):
                self.assertEqual(self.schema[key].get("condition"), {"ccb": True})

    def test_the_documented_followers_are_really_conditioned(self) -> None:
        for key, switch in FOLLOWERS.items():
            with self.subTest(key=key):
                self.assertIn(key, self.schema, f"{key} 不在 schema 里了")
                condition = self.schema[key].get("condition") or {}
                self.assertIn(
                    switch, condition, f"{key} 没有跟着主开关 {switch} 隐藏"
                )
                self.assertTrue(condition[switch])

    def test_secondary_switches_hide_with_their_own_group(self) -> None:
        """二级开关（情绪里的 LLM 判情绪/日志/降温）跟着 mood_enabled 走，一个不多一个不少。"""
        for key in ("mood_use_llm_for_delta", "mood_log_enabled", "cooling_enabled"):
            with self.subTest(key=key):
                self.assertEqual(
                    self.schema[key].get("condition"), {"mood_enabled": True}
                )

    def test_master_switches_stay_visible(self) -> None:
        for key in MASTER_SWITCHES:
            with self.subTest(key=key):
                self.assertIn(key, self.schema)
                self.assertNotIn(
                    "condition",
                    self.schema[key],
                    f"主开关 {key} 自己也被条件藏了，用户无法打开任何东西",
                )

    def test_conditions_reference_real_keys(self) -> None:
        for key, meta in self.schema.items():
            condition = meta.get("condition")
            if not condition:
                continue
            with self.subTest(key=key):
                self.assertIsInstance(condition, dict)
                for parent in condition:
                    self.assertIn(parent, self.schema, f"{key} 的条件引用了不存在的 {parent}")

    def test_condition_values_match_the_parent_type(self) -> None:
        """`!==` 比较：值写错类型（`true` vs `"true"`）等于永久隐藏那一项。"""
        for key, meta in self.schema.items():
            condition = meta.get("condition")
            if not condition:
                continue
            for parent, expected in condition.items():
                with self.subTest(key=key, parent=parent):
                    parent_default = self.schema[parent]["default"]
                    self.assertIsInstance(
                        expected,
                        type(parent_default),
                        f"{key} 用 {expected!r} 去比 {parent}（默认值 {parent_default!r}），"
                        "类型不同时前端 !== 永远不成立",
                    )

    def test_condition_chains_terminate(self) -> None:
        """条件键自己也可能有条件（子项挂在子开关下），不能成环。"""
        for key in self.schema:
            seen = [key]
            cursor = key
            while True:
                condition = self.schema[cursor].get("condition") or {}
                parents = [p for p in condition if p != cursor]
                if not parents:
                    break
                cursor = parents[0]
                self.assertNotIn(
                    cursor, seen, f"条件链成环：{' -> '.join(seen + [cursor])}"
                )
                seen.append(cursor)
                self.assertLess(len(seen), 8, f"条件链太长，可能写错了：{seen}")

    def test_conditioned_items_are_more_than_a_handful(self) -> None:
        """这一版至少要让「开关下面那堆从属项」都跟上：少于 60 项说明漏了一大片。"""
        conditioned = [k for k, v in self.schema.items() if v.get("condition")]
        self.assertGreaterEqual(len(conditioned), 60)


if __name__ == "__main__":
    unittest.main()
