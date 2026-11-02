"""v3.0.2：ccb 常驻那句必须和真正拦门的门槛说同一套话。

用户原话（m01896 / m01915）：「ccb 场景的使用要真的能用上（与好感方面），不要成为
一个功能摆件」。审计发现注入层那句「她此刻的欲望」只抄了亲近欲门槛，好感、好感涨幅、
精力、冷却一个都没算：好感 35 的人也会被告知「已经到了会忍不住的那条线」，而同一时刻
`ccb.evaluate()` 因为好感不够直接返回 None——写着有戏、实际永远进不去，这就是
「摆件」的由来。

这一版抽了 `ccb.gate_gaps()` 做**唯一**的数值门槛定义，`_values_hold()`（真拦门的）、
`scene_status()`（/亲密状态 面板）和注入层那句共用它。测试盯三件事：

* 有别的门槛没过时，绝不能宣称「已经到了会忍不住」；
* `gate_gaps()` 与 `_values_hold()` 逐格等价，不许再长出第二份判定；
* 说「忍不住」时 `ccb.evaluate()` 必须真放行——说的和做的是同一个数。
"""

from __future__ import annotations

import unittest

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.prompt_builder import PromptBuilder
from tests.test_v2287_ccb_presence import FEMALE, _core, _mood


def _conf(**raw) -> HumanoidConfig:
    data = {"timezone_city": "北京", "ccb": True}
    data.update(raw)
    return HumanoidConfig.from_raw(data)


def _desire(
    conf: HumanoidConfig,
    libido: float = 41.0,
    base_libido: float = 34.0,
    affection: float = 92.0,
    base_affection: float = 46.0,
    energy: float = 70.0,
    cooldown_left: float = 0.0,
) -> str:
    """只调这一句：`self` 在这条路径上没有用到，传 None 就够。"""
    return PromptBuilder._ccb_desire_presence(
        None,
        conf,
        libido,
        base_libido,
        affection=affection,
        base_affection=base_affection,
        energy=energy,
        cooldown_left=cooldown_left,
    )


def _evaluate(
    conf: HumanoidConfig,
    affection: float,
    base_affection: float,
    libido: float,
    base_libido: float,
    energy: float = 70.0,
):
    """和注入层同参数的 `evaluate()`：场景那一关按「模型说在亲近」算成立。"""
    return ccb.evaluate(
        enabled=True,
        is_group=False,
        persona_prompt=FEMALE,
        affection=affection,
        base_affection=base_affection,
        libido=libido,
        base_libido=base_libido,
        libido_min=getattr(conf, "ccb_libido_min", ccb.CCB_LIBIDO_MIN),
        libido_rise=getattr(conf, "ccb_libido_rise", ccb.CCB_LIBIDO_RISE),
        affection_min=getattr(conf, "ccb_affection_min", ccb.CCB_AFFECTION_MIN),
        affection_rise=getattr(conf, "ccb_affection_rise", ccb.CCB_AFFECTION_RISE),
        affection_deep=getattr(conf, "ccb_affection_deep", ccb.CCB_AFFECTION_DEEP),
        energy=energy,
        last_at=0.0,
        now=1_000_000.0,
        turns_today=0.0,
        intimacy_hint=True,
    )


CLAIM = "已经到了会忍不住"


class GateGapsAreTheSingleSource(unittest.TestCase):
    """面板 / 注入 / 状态机三处必须用同一份门槛，不再各抄一半。"""

    _KEYS = ("libido_gap", "libido_rise_gap", "affection_gap", "affection_rise_gap")

    def test_gate_gaps_agrees_with_values_hold(self):
        bad = []
        for aff in (0.0, 20.0, 35.0, 49.9, 50.0, 60.0, 74.9, 75.0, 92.0, 100.0):
            for drop in (-12.0, -4.0, 0.0, 3.0, 8.0):
                base_aff = max(0.0, aff + drop)
                for lib in (0.0, 20.0, 30.0, 37.9, 38.0, 41.0, 44.0, 49.0, 50.0):
                    for drop_lib in (-8.0, -4.0, 0.0, 2.0, 4.0):
                        base_lib = max(0.0, min(lib, lib + drop_lib))
                        gaps = ccb.gate_gaps(
                            affection=aff,
                            base_affection=base_aff,
                            libido=lib,
                            base_libido=base_lib,
                        )
                        hold = ccb._values_hold(
                            affection=aff,
                            base_affection=base_aff,
                            libido=lib,
                            base_libido=base_lib,
                        )
                        expect = all(gaps[key] <= 0.0 for key in self._KEYS)
                        if hold != expect:
                            bad.append((aff, base_aff, lib, base_lib, hold, gaps))
        self.assertEqual(bad, [], f"gate_gaps 与 _values_hold 走歪了：{bad[:3]}")

    def test_the_two_saturation_exceptions_survive(self):
        # 顶到量程 / 已进深入档：不再要求「这一阵还得涨」。
        self.assertTrue(ccb._values_hold(
            affection=80.0, base_affection=80.0, libido=50.0, base_libido=50.0))
        self.assertTrue(ccb._values_hold(
            affection=75.0, base_affection=75.0, libido=44.0, base_libido=40.0))
        gaps = ccb.gate_gaps(
            affection=35.0, base_affection=35.0, libido=41.0, base_libido=34.0)
        self.assertGreater(gaps["affection_gap"], 0.0)
        # 好感 35/35：涨幅要求照旧算满（没进深入档，不享受饱和例外）。
        self.assertGreater(gaps["affection_rise_gap"], 0.0)


class DesiredButNotYet(unittest.TestCase):
    """别的门槛没过时，这句只说「还没到，因为…」，不许说到了。"""

    def test_low_affection_does_not_claim_she_is_about_to_lose_it(self):
        text = _desire(_conf(), affection=35.0, base_affection=35.0)
        self.assertNotIn(CLAIM, text, text)
        self.assertIn("她此刻的欲望", text)
        self.assertIn("好感 35/100", text, text)
        self.assertIn("关系还没到那一步", text, text)

    def test_low_energy_says_so(self):
        text = _desire(_conf(), energy=20.0)
        self.assertNotIn(CLAIM, text, text)
        self.assertIn("精力见底（20/100）", text, text)

    def test_cooldown_says_so(self):
        text = _desire(_conf(), cooldown_left=1500.0)
        self.assertNotIn(CLAIM, text, text)
        self.assertIn("刚收场不久", text, text)
        self.assertIn("还要等", text, text)

    def test_affection_rise_shortfall_says_so(self):
        text = _desire(_conf(), affection=52.0, base_affection=48.0)
        self.assertNotIn(CLAIM, text, text)
        self.assertIn("这份亲近还没攒够", text, text)

    def test_the_old_text_is_unchanged_when_every_gate_is_met(self):
        # 好感 92 已在深入档、亲近欲 41/34 过 38/Δ4：与 v2.28.x 的句子逐字一致。
        text = _desire(_conf())
        self.assertIn("她此刻的欲望：亲近欲 41/50（比平常高 7）", text, text)
        self.assertIn(CLAIM, text, text)

    def test_below_baseline_keeps_the_plain_conclusion(self):
        text = _desire(_conf(), libido=22.0, base_libido=34.0)
        self.assertEqual(text, "她此刻没有那个心思（亲近欲 22/50，比平常低 12）")


class ClaimImpliesTheSceneWouldOpen(unittest.TestCase):
    """说「忍不住」的那一档，`evaluate()` 必须真的返回放行。"""

    def test_no_false_claim_over_the_grid(self):
        conf = _conf()
        checked = 0
        for aff, base_aff in ((35.0, 35.0), (50.0, 44.0), (52.0, 48.0),
                              (60.0, 46.0), (74.0, 46.0), (75.0, 46.0), (92.0, 46.0)):
            for lib, base_lib in ((38.0, 34.0), (41.0, 34.0), (44.0, 40.0), (50.0, 50.0)):
                for energy in (20.0, 70.0):
                    text = _desire(
                        conf,
                        libido=lib,
                        base_libido=base_lib,
                        affection=aff,
                        base_affection=base_aff,
                        energy=energy,
                    )
                    if CLAIM not in text:
                        continue
                    checked += 1
                    self.assertIsNotNone(
                        _evaluate(conf, aff, base_aff, lib, base_lib, energy),
                        f"注入说她已经忍不住，evaluate 却挡住：{text}",
                    )
        self.assertGreater(checked, 0, "网格里一个「忍不住」都没出现，断言等于没跑")


class PresenceLineWiring(unittest.TestCase):
    """真链路：`build_injection` 取到的是档案里的好感 / 精力，不是写死的默认值。"""

    def test_the_injected_line_carries_the_affection_gate(self):
        core = _core()
        _mood(core, affection=35.0, base_affection=35.0,
              libido=41.0, base_libido=34.0)
        text = core.build_injection("u1")
        self.assertIn("好感 35/100", text)
        self.assertNotIn(CLAIM, text)

    def test_with_deep_affection_it_still_reads_as_before(self):
        core = _core()
        _mood(core)
        text = core.build_injection("u1")
        self.assertIn("亲近欲 41/50", text)
        self.assertIn(CLAIM, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
