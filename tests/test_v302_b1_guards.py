"""B1 批次守卫：配置解析与面板契约的对齐。

对应审计发现：F3（出厂默认下「过深」永不可达）、F5（数值项没有面板上下限）、
F6（长文本被静默截断）、F7（倍率表长度）、F8（energy_decay_rate 曾是字符串枚举）、
F9/F12（面板 hint 与代码实际边界不符）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from humanoid.config import HumanoidConfig

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class NumericRangeTest(unittest.TestCase):
    """F5：面板放行任意数值时，解析层夹住必须留一条日志。"""

    def test_max_energy_out_of_range_is_clamped_and_logged(self) -> None:
        with self.assertLogs("humanoid.config", level="WARNING") as caught:
            cfg = HumanoidConfig.from_raw({"max_energy": 0})
        self.assertEqual(cfg.max_energy, 1.0)
        self.assertIn("超出", "\n".join(caught.output))

    def test_ccb_thresholds_are_clamped(self) -> None:
        cases = {
            "ccb_libido_min": (999, 50.0),
            "ccb_libido_rise": (-5, 0.0),
            "ccb_affection_min": (101, 100.0),
            "ccb_affection_rise": (999, 50.0),
            "ccb_affection_deep": (-1, 0.0),
        }
        for key, (raw, want) in cases.items():
            with self.subTest(key=key):
                with self.assertLogs("humanoid.config", level="WARNING"):
                    cfg = HumanoidConfig.from_raw({key: raw})
                self.assertEqual(getattr(cfg, key), want)

    def test_numeric_settings_declare_their_panel_range(self) -> None:
        schema = _schema()
        for key in (
            "max_energy",
            "energy_decay_rate",
            "ccb_libido_min",
            "ccb_libido_rise",
            "ccb_affection_min",
            "ccb_affection_rise",
            "ccb_affection_deep",
        ):
            with self.subTest(key=key):
                self.assertIn("minimum", schema[key])
                self.assertIn("maximum", schema[key])


class TruncationTest(unittest.TestCase):
    """F6：自由文本超长会被截断，不能静默。"""

    def test_schedule_prompt_extra_is_truncated_with_a_warning(self) -> None:
        with self.assertLogs("humanoid.config", level="WARNING") as caught:
            cfg = HumanoidConfig.from_raw({"schedule_prompt_extra": "安" * 400})
        self.assertEqual(
            len(cfg.schedule_prompt_extra), HumanoidConfig.SCHEDULE_PROMPT_EXTRA_MAX
        )
        self.assertIn("截断", "\n".join(caught.output))

    def test_outfit_preset_is_truncated_with_a_warning(self) -> None:
        with self.assertLogs("humanoid.config", level="WARNING"):
            cfg = HumanoidConfig.from_raw({"outfit_preset": "裙" * 260})
        self.assertEqual(len(cfg.outfit_preset), 200)


class SchemaContractTest(unittest.TestCase):
    """F3/F4/F7/F8/F9/F12：面板承诺与代码行为对齐。"""

    def test_default_length_can_go_over_the_cap(self) -> None:
        schema = _schema()
        self.assertGreater(
            schema["ccb_partner_length_cm"]["default"],
            schema["ccb_body_depth_cap_cm"]["default"],
            "出厂默认必须能触发「过深」，否则那一档永不出现",
        )
        cfg = HumanoidConfig.from_raw({})
        self.assertGreater(cfg.ccb_partner_length_cm, cfg.ccb_body_depth_cap_cm)

    def test_contract_enabled_is_not_hidden_by_the_body_switch(self) -> None:
        self.assertNotIn("condition", _schema()["contract_enabled"])

    def test_phase_multiplier_list_is_fixed_at_eight(self) -> None:
        meta = _schema()["energy_recovery_phase_multipliers"]
        self.assertEqual(meta.get("minItems"), 8)
        self.assertEqual(meta.get("maxItems"), 8)

    def test_energy_decay_rate_is_a_number_not_an_enum(self) -> None:
        meta = _schema()["energy_decay_rate"]
        self.assertEqual(meta["type"], "float")
        self.assertNotIn("options", meta)
        self.assertEqual(
            HumanoidConfig.from_raw({"energy_decay_rate": "2.0"}).energy_decay_rate, 2.0
        )
        with self.assertLogs("humanoid.config", level="WARNING"):
            cfg = HumanoidConfig.from_raw({"energy_decay_rate": 9})
        self.assertEqual(cfg.energy_decay_rate, 3.0)

    def test_retention_zero_is_documented_as_never(self) -> None:
        self.assertIn("永不清理", _schema()["mood_data_retention_days"]["hint"])

    def test_energy_hints_match_their_real_lower_bound(self) -> None:
        schema = _schema()
        for key in ("energy_natural_recovery_per_minute", "energy_consumption_per_msg"):
            with self.subTest(key=key):
                self.assertEqual(schema[key]["minimum"], 0)
                self.assertIn("0~0.9", schema[key]["hint"])


if __name__ == "__main__":
    unittest.main()
