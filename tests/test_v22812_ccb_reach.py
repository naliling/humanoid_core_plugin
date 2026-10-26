"""CCB 门槛可达性（v2.28.12）。

根因：绝对门槛 38/50 是按「默认基线 34 + 每日最多涨 4.5」配的。面板把初始亲近欲
调低（本机是 30）之后，38 永远够不着 —— 线上实测 8562 个用户条目里没有一个人
进过场景，ccb_gate_at 一次都没被写过。这里把「不是死开关」这件事钉住：
  · 配置门槛高于「基线 + 每日可达涨幅」时按可达值算；
  · 顶到量程（50）算「她一直很想要」，不再要求还能再涨；
  · 好感进了深入档（75）算「关系已经够深」，不再要求这一阵涨过 8 分。
"""
import unittest

from humanoid import ccb

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"


def _ok(**kw):
    base = dict(enabled=True, is_group=False, persona_prompt=FEMALE,
                affection=92.0, base_affection=46.0,
                libido=47.0, base_libido=34.0,
                energy=70.0, last_at=0.0, now=1_000_000.0)
    base.update(kw)
    return base


def _snap(**kw):
    base = dict(libido=47.0, base_libido=34.0, affection=92.0, base_affection=46.0,
                energy=70.0, intimacy_hint=True)
    base.update(kw)
    return ccb.scene_status({}, 1_000_000.0, **base)


class ReachableThreshold(unittest.TestCase):
    def test_low_baseline_is_clamped_to_the_reachable_ceiling(self):
        # 基线 30 + 每日最多涨 6 = 最高 36；配置的 38 永远够不着
        self.assertAlmostEqual(ccb.reachable_libido_min(38.0, 30.0), 36.0)

    def test_normal_baseline_keeps_the_configured_threshold(self):
        # 默认基线 34 时可达 40，38 本来就是能到的高度 —— 不夹
        self.assertAlmostEqual(ccb.reachable_libido_min(38.0, 34.0), 38.0)

    def test_lower_config_is_not_raised(self):
        # 用户刻意调低门槛（20，可达 26）时原样保留，不被抬回去
        self.assertAlmostEqual(ccb.reachable_libido_min(20.0, 20.0), 20.0)

    def test_bad_baseline_falls_back_to_config(self):
        self.assertAlmostEqual(ccb.reachable_libido_min(38.0, float("nan")), 38.0)


class GateIsReachable(unittest.TestCase):
    def test_low_baseline_user_can_enter_after_rising(self):
        # 基线 30、涨到 36.5（+6.5）：旧门槛 38 永远进不来，现在应该能进
        self.assertIsNotNone(ccb.evaluate(**_ok(libido=36.5, base_libido=30.0)))

    def test_still_requires_being_at_the_top(self):
        # 35.9 < 36：还没到今天的顶，不该进
        self.assertIsNone(ccb.evaluate(**_ok(libido=35.9, base_libido=30.0)))

    def test_saturated_libido_counts_as_risen(self):
        # 顶到量程的人（50/50）不可能再涨，但那一刻本来就是「一直很想要」
        self.assertIsNotNone(
            ccb.evaluate(**_ok(libido=50.0, base_libido=49.97)),
            "顶到量程的用户不该被「涨得不够」挡在门外",
        )

    def test_deep_affection_counts_as_close_enough(self):
        # 好感 92/基线 90：涨 2 分，但已经进了深入档（75）
        self.assertIsNotNone(ccb.evaluate(**_ok(affection=92.0, base_affection=90.0)))

    def test_rising_affection_still_required_below_deep(self):
        # 60/56：涨 4 分、没到 75，仍然算「不够」
        self.assertIsNone(ccb.evaluate(**_ok(affection=60.0, base_affection=56.0)))


class StatusIsHonest(unittest.TestCase):
    def test_status_shows_the_effective_threshold(self):
        snap = _snap(libido=31.5, base_libido=30.0, libido_min=38.0)
        self.assertAlmostEqual(snap["libido_min"], 36.0)
        self.assertAlmostEqual(snap["libido_min_config"], 38.0)
        text = "；".join(snap["blockers"])
        self.assertIn("超出她这具身体一天能涨到的上限", text)

    def test_deep_affection_is_not_blocked_in_status_either(self):
        snap = _snap(affection=92.0, base_affection=90.0)
        self.assertTrue(snap["can_enter"], snap["blockers"])


class SceneStaysOpen(unittest.TestCase):
    def test_lapsed_uses_the_same_reachable_threshold(self):
        kw = dict(is_group=False, persona_prompt=FEMALE,
                  affection=92.0, base_affection=46.0,
                  libido=36.5, base_libido=30.0, libido_min=38.0)
        self.assertFalse(ccb.scene_lapsed(**kw), "36.5 已到可达门槛，场景不该被收")
        self.assertTrue(ccb.scene_lapsed(**{**kw, "libido": 30.0}))
