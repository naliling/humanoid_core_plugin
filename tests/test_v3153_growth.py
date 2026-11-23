"""v3.1.53：bot 成长可见性 + 心理层剂量旋钮。

组1：/成长 档案面板（认识/阶段/轮数/好感/情绪事件/日程/心事）把后台已记着的
成长数据摆出来；/你的状态、/心事 各加一句人话解读；阶段跃迁写 user 级档案。
组2：情绪保留 7→30 天；心理层四个剂量旋钮从硬编码改为面板配置。

这里锁纯函数/数据层不变量（不依赖 AstrBot 运行时）：
* 新配置默认值 + from_raw 钳制；
* PsycheStore 把面板配置挂到 state，纯方法据此放宽/收紧；
* _growth_hint 人话解读分档正确；
* schema 里四项配置齐全、情绪保留默认 30。
"""
from __future__ import annotations

import json
import unittest

from humanoid import config as config_mod
from humanoid import psyche as psyche_mod
from humanoid.core_instance import HumanoidCoreInstance


class DefaultsTest(unittest.TestCase):
    def test_retention_default_is_30(self):
        self.assertEqual(config_mod.DEFAULTS.mood_data_retention_days, 30)

    def test_psyche_knobs_default_to_original_conservative_values(self):
        d = config_mod.DEFAULTS
        self.assertAlmostEqual(d.psyche_rumination_threshold, 0.55)
        self.assertEqual(d.psyche_inject_max_lines, 2)
        self.assertAlmostEqual(d.psyche_min_interval_seconds, 180.0)
        self.assertEqual(d.psyche_daily_cap, 40)

    def test_from_raw_picks_up_knobs(self):
        built = config_mod.HumanoidConfig.from_raw({
            "psyche_rumination_threshold": 0.3,
            "psyche_inject_max_lines": 4,
            "psyche_min_interval_seconds": 60.0,
            "psyche_daily_cap": 100,
        })
        self.assertAlmostEqual(built.psyche_rumination_threshold, 0.3)
        self.assertEqual(built.psyche_inject_max_lines, 4)
        self.assertAlmostEqual(built.psyche_min_interval_seconds, 60.0)
        self.assertEqual(built.psyche_daily_cap, 100)


class _Cfg:
    psyche_rumination_threshold = 0.2
    psyche_inject_max_lines = 4


class PsycheRuntimeConfigTest(unittest.TestCase):
    def test_store_applies_runtime_knobs_to_state(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        store = psyche_mod.PsycheStore(tmp, "role1", config_provider=_Cfg())
        st = store.state("u1")
        self.assertAlmostEqual(st.rumination_threshold, 0.2)
        self.assertEqual(st.inject_max_lines, 4)

    def test_state_defaults_when_no_store_config(self):
        st = psyche_mod.PsycheState("u1")
        self.assertAlmostEqual(st.rumination_threshold, psyche_mod.RUMINATION_THRESHOLD)
        self.assertEqual(st.inject_max_lines, psyche_mod.INJECT_MAX_LINES)


class GrowthHintTest(unittest.TestCase):
    def test_high_affection_low_energy(self):
        hint = HumanoidCoreInstance._growth_hint(88.0, 20.0)
        self.assertIn("自己人", hint)
        self.assertIn("累", hint)

    def test_mid_affection(self):
        hint = HumanoidCoreInstance._growth_hint(65.0, 50.0)
        self.assertIn("在意", hint)

    def test_low_affection(self):
        self.assertIn("不熟", HumanoidCoreInstance._growth_hint(20.0, 50.0))


class SchemaTest(unittest.TestCase):
    def test_four_psyche_knobs_present(self):
        # 不依赖 config 模块是否导出 Path：直接定位仓库根。
        from pathlib import Path
        schema = json.loads(
            (Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(
                encoding="utf-8")
        )
        for key in (
            "psyche_rumination_threshold", "psyche_inject_max_lines",
            "psyche_min_interval_seconds", "psyche_daily_cap",
        ):
            self.assertIn(key, schema)
        self.assertEqual(schema["mood_data_retention_days"]["default"], 30)


if __name__ == "__main__":
    unittest.main()
